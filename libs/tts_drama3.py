"""
Fish Audio Drama 3 through speech.cpp.

Drama 3 (fishaudio/drama-3, 35B MoE, 3B active) runs in speech.cpp's native ggml
port, which keeps as many expert layers on the GPU as fit and computes the rest
on the CPU, so the Q4_K file runs on a 16 GB card.

- build_voice() encodes a reference clip + transcript into a reusable
  .voice.gguf (speech.cpp's pre-encoded reference, valid for every Drama 3 file).
- Drama3Worker keeps one `speech worker` process (model loaded once) and speaks
  texts over its JSON Lines protocol.

Paths come from the environment (SPEECH_EXE, DRAMA3_MODEL) or the defaults below.
"""

import base64
import json
import logging
import os
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ==================== CONFIGURATION ====================
SPEECH_EXE = Path(os.environ.get("SPEECH_EXE", r"D:\fish-drama3\speech\speech.exe"))
# Keep the model on an SSD: it loads in ~25 s from NVMe against ~4 min from a hard disk
DRAMA3_MODEL = Path(os.environ.get(
    "DRAMA3_MODEL", r"C:\fish-drama3\models\drama-3-final\Drama-35B-A3B-v3-Q4_K.gguf"))
DRAMA3_DEVICE = os.environ.get("DRAMA3_DEVICE", os.environ.get("SPEECH_DEVICE", "4060 Ti"))
WORKER_LOG = Path("logs/drama3_worker.log")
# Drama 3 stalls in silence at sentence ends with a small top-k; 4097 keeps every candidate (Fish's own Space does this)
DEFAULT_REQUEST_OPTIONS = {"top_k": 4097, "top_p": 0.9, "temperature": 1.0}
# =======================================================

_CACHED_DEVICES: Optional[List[Dict]] = None


def get_speech_devices(exe: Path = SPEECH_EXE) -> List[Dict]:
    """
    List compute devices supported by speech.exe.
    Queries speech.dll via ctypes (fast, ~5 ms) or speech devices --json CLI as fallback.
    """
    global _CACHED_DEVICES
    if _CACHED_DEVICES is not None:
        return _CACHED_DEVICES

    devices: List[Dict] = []
    dll_path = exe.parent / "speech.dll"
    if dll_path.exists():
        try:
            import ctypes
            dll = ctypes.CDLL(str(dll_path))
            dll.speech_device_count.restype = ctypes.c_size_t
            dll.speech_device_name.restype = ctypes.c_char_p
            dll.speech_device_name.argtypes = [ctypes.c_size_t]
            dll.speech_device_description.restype = ctypes.c_char_p
            dll.speech_device_description.argtypes = [ctypes.c_size_t]
            dll.speech_device_get_kind.argtypes = [ctypes.c_size_t, ctypes.POINTER(ctypes.c_int)]
            dll.speech_device_memory.argtypes = [ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64)]

            count = dll.speech_device_count()
            kind_map = {0: "cpu", 1: "gpu", 2: "igpu"}
            for i in range(count):
                name = dll.speech_device_name(i).decode("utf-8")
                desc = dll.speech_device_description(i).decode("utf-8")
                kind_val = ctypes.c_int()
                dll.speech_device_get_kind(i, ctypes.byref(kind_val))
                total = ctypes.c_uint64()
                free = ctypes.c_uint64()
                dll.speech_device_memory(i, ctypes.byref(total), ctypes.byref(free))
                devices.append({
                    "name": name,
                    "description": desc,
                    "kind": kind_map.get(kind_val.value, "unknown"),
                    "memory_total": total.value,
                    "memory_free": free.value,
                })
        except Exception as e:
            logger.debug(f"ctypes speech device check failed: {e}")
            devices = []

    if not devices and exe.exists():
        try:
            res = subprocess.run([str(exe), "devices", "--json"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=5, env=speech_env())
            if res.returncode == 0 and res.stdout.strip():
                devices = json.loads(res.stdout).get("devices", [])
        except Exception as e:
            logger.debug(f"CLI speech device check failed: {e}")

    _CACHED_DEVICES = devices
    return devices


def resolve_device(target: Optional[str] = None, exe: Path = SPEECH_EXE) -> str:
    """
    Resolve a device name, description, or preference to a device name accepted by speech.exe.
    Defaults to DRAMA3_DEVICE env var or '4060 Ti'.
    """
    target = target or DRAMA3_DEVICE or "4060 Ti"
    target_clean = str(target).strip()
    target_lower = target_clean.lower()

    if target_lower in ("auto", "gpu", "cpu"):
        return target_lower

    devices = get_speech_devices(exe)
    if not devices:
        return target_clean

    # 1. Exact match on device name (case-insensitive, e.g. "Vulkan1")
    for d in devices:
        if d.get("name", "").lower() == target_lower:
            return d["name"]

    # 2. Substring match on description or name (e.g. "4060", "4060 Ti", "1660")
    for d in devices:
        desc = d.get("description", "").lower()
        if target_lower in desc:
            return d["name"]

    # 3. If target was 4060 or not matched, fall back to GPU with largest memory
    gpus = [d for d in devices if d.get("kind") == "gpu"]
    if gpus:
        best_gpu = max(gpus, key=lambda d: d.get("memory_total", 0))
        return best_gpu["name"]

    return target_clean


@dataclass
class Take:
    """
    One synthesized text.

    Attributes:
        pcm: 16-bit little-endian mono samples.
        sample_rate: Rate of `pcm` (44100 for Drama 3).
        seed: Seed the take used (given or drawn by speech.cpp).
        stop: "complete", or "max_seconds" / "model_limit" if it was cut off.
    """
    pcm: bytes
    sample_rate: int
    seed: int
    stop: str

    @property
    def seconds(self) -> float:
        """Duration of the take."""
        return len(self.pcm) / 2 / self.sample_rate

    def save(self, path: Path) -> None:
        """Write the take as a 16-bit mono WAV."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(self.pcm)


class WorkerError(RuntimeError):
    """A request the worker answered with `error` or `fatal`, or a worker that died."""


def speech_env() -> Dict[str, str]:
    """Environment for speech.exe: no update check (keeps runs offline and quiet)."""
    return {**os.environ, "SPEECH_NO_UPDATE_CHECK": "1"}


def build_voice(recordings: List[Tuple[Path, str]], out: Path, model: Path = DRAMA3_MODEL,
                exe: Path = SPEECH_EXE) -> Path:
    """
    Encode reference recordings of one speaker into a Drama 3 voice file.

    The voice file holds the codec's codes of each recording and its words, so
    the model never re-encodes the audio. Several recordings give the model
    more of the speaker's range, at the cost of a longer prompt per request.
    It is rebuilt only when a recording is newer than it.

    Args:
        recordings: (wav, transcript) pairs; each transcript is exactly the words spoken.
        out: Destination `.voice.gguf`.
        model: Drama 3 GGUF (only its codec is used).
        exe: speech.exe.

    Returns:
        `out`.

    Raises:
        WorkerError: speech.exe failed.
    """
    if out.exists() and all(out.stat().st_mtime >= wav.stat().st_mtime for wav, _ in recordings):
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(exe), "voice", str(model)] + [str(wav) for wav, _ in recordings]
    for _, transcript in recordings:
        cmd += ["--transcript", transcript.strip()]
    cmd.append(str(out))
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            env=speech_env())
    if result.returncode != 0:
        out.unlink(missing_ok=True)
        raise WorkerError(f"speech voice failed for {out.name}: {result.stderr.strip()[-500:]}")
    logger.info(f"🎤 Voice file {out} ({len(recordings)} recording(s))")
    return out


class Drama3Worker:
    """
    A `speech worker` process serving Drama 3 over JSON Lines.

    Loading takes 15-30 s; keep one worker for a whole batch. speech.cpp keeps a
    voice's reference prompt cached until a request uses another voice, so send
    requests grouped by voice.

    Example:
        >>> with Drama3Worker() as worker:
        ...     worker.add_voice("morte", Path("morte.voice.gguf"))
        ...     worker.synthesize("[dry] Ugly little berks.", "morte", seed=1).save(Path("out.wav"))
    """

    def __init__(self, model: Path = DRAMA3_MODEL, exe: Path = SPEECH_EXE, threads: Optional[int] = None,
                 device: Optional[str] = None, log_path: Path = WORKER_LOG):
        if not exe.exists():
            raise FileNotFoundError(f"speech.exe not found: {exe} (set SPEECH_EXE)")
        if not model.exists():
            raise FileNotFoundError(f"Drama 3 model not found: {model} (set DRAMA3_MODEL)")
        chosen_device = resolve_device(device, exe=exe)
        cmd = [str(exe), "worker", str(model)]
        if threads:
            cmd += ["--threads", str(threads)]
        if chosen_device:
            cmd += ["--device", chosen_device]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log: IO[bytes] = log_path.open("ab")
        logger.info(f"🐟 Loading Drama 3 ({model.name}) on {chosen_device} ...")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
                                     env=speech_env())
        self.voices: set = set()
        self._next_id = 0
        ready = self._read()
        if ready.get("type") != "ready":
            self.close()
            raise WorkerError(f"worker did not start: {ready}")
        info = ready.get("model", {})
        self.sample_rate = int(info.get("sample_rate") or 44100)
        self.device = info.get("device", chosen_device or "?")
        logger.info(f"🐟 Drama 3 ready on {self.device} ({self.sample_rate} Hz)")

    def __enter__(self) -> "Drama3Worker":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _send(self, message: Dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _read(self) -> Dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            raise WorkerError(f"worker exited (code {self.proc.poll()}); see {WORKER_LOG}")
        message = json.loads(line.decode("utf-8"))
        if message.get("type") == "fatal":
            raise WorkerError(f"worker fatal: {message.get('error')}")
        return message

    def _request_id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def add_voice(self, name: str, voice_file: Path) -> None:
        """Make a voice file available to later requests as `name` (no-op if already added)."""
        if name in self.voices:
            return
        rid = self._request_id()
        self._send({"type": "add_voice", "id": rid, "name": name, "path": str(voice_file.resolve())})
        while True:
            message = self._read()
            if message.get("id") != rid:
                continue
            if message["type"] == "error":
                raise WorkerError(f"add_voice {name}: {message.get('error')}")
            if message["type"] == "end":
                self.voices.add(name)
                return

    def synthesize(self, text: str, voice: str, seed: Optional[int] = None,
                   max_seconds: Optional[float] = None, **options) -> Take:
        """
        Speak one text in an added voice.

        Args:
            text: Text with Drama 3 directions, e.g. "[weary] Fine. <emphasis>Fine.</emphasis>".
            voice: Name given to add_voice().
            seed: Sampling seed (same seed + device = same take).
            max_seconds: Cut the take off after this long (guards runaway silences).
            **options: Other request options (top_k, top_p, temperature).

        Returns:
            The take.

        Raises:
            WorkerError: The worker refused the request or died.
        """
        rid = self._request_id()
        request = {"type": "synthesize", "id": rid, "text": text, "voice": voice,
                   **DEFAULT_REQUEST_OPTIONS, **options}
        if seed is not None:
            request["seed"] = seed
        if max_seconds:
            request["max_seconds"] = round(float(max_seconds), 2)
        self._send(request)
        pcm = bytearray()
        while True:
            message = self._read()
            if message.get("id") != rid:
                continue
            kind = message["type"]
            if kind == "chunk":
                pcm += base64.b64decode(message["pcm"])
            elif kind == "end":
                return Take(bytes(pcm), self.sample_rate, int(message.get("seed", seed or 0)),
                            message.get("stop", "complete"))
            elif kind in ("error", "cancelled"):
                raise WorkerError(f"synthesize failed: {message.get('error', kind)}")

    def close(self) -> None:
        """Close stdin (the worker finishes and exits) and wait for it."""
        if self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.wait(timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
        self._log.close()
