#!/usr/bin/env python3
"""
PST Farm Worker: speaks Fish Audio Drama 3 lines for the master PC.

Runs on each farm PC from its pst_farm folder (copied there by "Update farm
PCs" in the master's PST Revoice window), and on the master itself as
"This PC". It watches queue/inbox for batches the master sends, speaks every
segment with the same seeds, retries and checks as pst_fish.py, and writes
the takes to queue/done for the master to pull back and stitch.

    run_worker.bat                      window with progress (farm PCs)
    python pst_farm_worker.py --root D:\\vve\\pst_farm
    python pst_farm_worker.py --root pst_revoice/farm/local --headless --serve ^
        --master-state pst_revoice/farm/state.json

The window shows whether this PC is serving, the line being spoken, batch
progress and counters. "Stop serving" finishes the current segment and hands
the rest of the batch back. The model is unloaded after a while with no work,
so the GPU is free for games.
"""

import argparse
import logging
import os
import shutil
import socket
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from libs.farm import HEARTBEAT_SECONDS, PROTOCOL_VERSION, FarmPaths, read_json, write_json  # noqa: E402

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8")

# ==================== CONFIGURATION ====================
DEFAULT_CONFIG = {
    "name": "",                   # shown on the master; empty = computer name
    "device": "",                 # speech.cpp device ("Vulkan0", "3070", ...); empty = biggest GPU
    "threads": 0,                 # CPU threads for experts the GPU can't hold; 0 = speech.cpp default
    "model": "",                  # Drama 3 GGUF path; empty = engine/models/*.gguf
    "idle_unload_minutes": 10,    # free the GPU after this long without work
    "serve_on_start": True,       # start serving as soon as the window opens
}
POLL_SECONDS = 2.0
MASTER_TIMEOUT = 90.0             # --master-state: stop when the master stops updating it
RATE_WINDOW = 1800.0              # seconds of history for the segments/minute figure
ERROR_BACKOFF = 300.0             # after a failure (e.g. not enough RAM) wait this long before taking work again
# =======================================================

logger = logging.getLogger("farm_worker")


def default_root() -> Path:
    """The pst_farm folder: the parent of app/ when run from a farm install."""
    return HERE.parent if HERE.name == "app" else HERE / "pst_revoice" / "farm" / "local"


def load_config(paths: FarmPaths) -> Dict:
    """worker-config.json merged over the defaults (written with the defaults if missing)."""
    data = read_json(paths.config)
    if not isinstance(data, dict):
        data = {}
        write_json(paths.config, DEFAULT_CONFIG)
    return {**DEFAULT_CONFIG, **data}


def configure_engine(paths: FarmPaths, config: Dict) -> None:
    """Point libs.tts_drama3 at this PC's speech.exe and model (env beats config beats engine/)."""
    if not os.environ.get("SPEECH_EXE") and paths.speech_exe.exists():
        os.environ["SPEECH_EXE"] = str(paths.speech_exe)
    if not os.environ.get("DRAMA3_MODEL"):
        model = Path(config["model"]) if config.get("model") else None
        if model is None or not model.exists():
            found = sorted(p for p in paths.models.glob("*.gguf") if not p.name.endswith(".voice.gguf"))
            model = found[0] if found else model
        if model is not None:
            os.environ["DRAMA3_MODEL"] = str(model)
    if config.get("device"):
        os.environ["DRAMA3_DEVICE"] = str(config["device"])
    elif not os.environ.get("DRAMA3_DEVICE"):
        os.environ["DRAMA3_DEVICE"] = "gpu-largest"   # resolve_device: no match -> GPU with the most memory


class FarmWorker:
    """The serving loop (a thread) plus the state the window and status.json show."""

    def __init__(self, paths: FarmPaths, config: Dict, master_state: Optional[Path] = None):
        from libs import tts_drama3
        self.tts = tts_drama3
        self.paths = paths
        self.config = config
        self.name = config.get("name") or socket.gethostname()
        self.master_state = master_state
        self.serving = bool(config.get("serve_on_start"))
        self.quit = False
        self.phase = "starting"
        self.error = ""
        self.engine = None                     # tts_drama3.Drama3Worker while the model is loaded
        self.loaded_voices: Dict[str, str] = {}  # key -> worker voice name (key~mtime)
        self.current: Dict = {}
        self.batch: Dict = {}
        self.counts = {"segments": 0, "lines": 0, "batches": 0, "audio_seconds": 0.0, "retries": 0, "errors": 0}
        self.started = time.time()
        self.idle_since = time.time()
        self.retry_at = 0.0                     # no work before this (after an error)
        self.done_times: Deque[float] = deque()
        self.segment_seconds: Deque[float] = deque(maxlen=50)
        self.log: Deque[str] = deque(maxlen=300)
        self.lock = threading.Lock()
        self.device = "?"
        self.gpus = self._gpu_names()

    # ---------------- state ----------------

    def _gpu_names(self) -> List[str]:
        try:
            return [f"{d.get('description') or d.get('name')} ({d.get('memory_total', 0) / 2**30:.0f} GB)"
                    for d in self.tts.get_speech_devices(self.tts.SPEECH_EXE) if d.get("kind") == "gpu"]
        except Exception:  # noqa: BLE001 - informational only
            return []

    def note(self, message: str) -> None:
        logger.info(message)
        with self.lock:
            self.log.append(f"{time.strftime('%H:%M:%S')} {message}")

    def rate(self) -> float:
        """Segments per minute over the last RATE_WINDOW seconds."""
        now = time.time()
        while self.done_times and now - self.done_times[0] > RATE_WINDOW:
            self.done_times.popleft()
        if len(self.done_times) < 2:
            return 0.0
        span = max(60.0, now - self.done_times[0])
        return len(self.done_times) / span * 60

    def snapshot(self) -> Dict:
        """What status.json and the window show."""
        with self.lock:
            return {
                "protocol": PROTOCOL_VERSION,
                "name": self.name,
                "host": socket.gethostname(),
                "time": time.time(),
                "started": self.started,
                "serving": self.serving,
                "phase": self.phase,
                "error": self.error,
                "model_loaded": self.engine is not None,
                "device": self.device,
                "gpus": self.gpus,
                "batch": dict(self.batch),
                "current": dict(self.current),
                "counts": dict(self.counts),
                "segments_per_minute": round(self.rate(), 2),
                "seconds_per_segment": round(sum(self.segment_seconds) / len(self.segment_seconds), 1)
                if self.segment_seconds else 0,
                "inbox": self.inbox_count(),
                "log": list(self.log)[-12:],
            }

    def inbox_count(self) -> int:
        try:
            return sum(1 for _ in self.paths.inbox.glob("*.json"))
        except OSError:
            return 0

    def heartbeat_loop(self) -> None:
        while not self.quit:
            try:
                write_json(self.paths.status, self.snapshot())
            except OSError as e:
                logger.warning(f"status.json: {e}")
            time.sleep(HEARTBEAT_SECONDS)
        write_json(self.paths.status, {**self.snapshot(), "serving": False, "phase": "closed"})

    # ---------------- model ----------------

    def load_model(self) -> None:
        if self.engine is not None:
            return
        self.phase = "loading"
        self.note(f"Loading Drama 3 ({Path(self.tts.DRAMA3_MODEL).name}); 20 s from an SSD, minutes from a hard disk")
        threads = int(self.config.get("threads") or 0) or None
        self.engine = self.tts.Drama3Worker(model=self.tts.DRAMA3_MODEL, exe=self.tts.SPEECH_EXE, threads=threads,
                                            device=os.environ.get("DRAMA3_DEVICE"),
                                            log_path=self.paths.root / "logs" / "drama3_worker.log")
        self.device = str(self.engine.device)
        self.loaded_voices = {}
        self.note(f"Drama 3 ready on {self.device}")

    def unload_model(self) -> None:
        engine, self.engine = self.engine, None
        if engine is not None:
            engine.close()
            self.note("Model unloaded (GPU free)")
        self.loaded_voices = {}

    def voice_name(self, key: str, info: Dict) -> str:
        """Add the voice file to the engine (once per file version) and return its engine name."""
        path = self.paths.voices / info["file"]
        if not path.exists():
            raise FileNotFoundError(f"voice file missing: {path}")
        name = f"{key}~{int(path.stat().st_mtime)}"
        if self.loaded_voices.get(key) != name:
            self.engine.add_voice(name, path)
            self.loaded_voices[key] = name
        return name

    # ---------------- queue ----------------

    def recover(self) -> None:
        """After a crash: claimed batches go back to the inbox, half-written results are dropped."""
        self.paths.make_dirs()
        for path in self.paths.working.glob("*.json"):
            os.replace(path, self.paths.inbox / path.name)
            self.note(f"Re-queued unfinished batch {path.stem}")
        for partial in self.paths.done.glob("*.partial"):
            shutil.rmtree(partial, ignore_errors=True)

    def claim(self) -> Optional[Path]:
        """Move the oldest inbox batch to working/ (the claim); None if there is none."""
        for path in sorted(self.paths.inbox.glob("*.json")):
            target = self.paths.working / path.name
            try:
                os.replace(path, target)
                return target
            except OSError:
                continue
        return None

    def run_batch(self, claimed: Path) -> None:
        batch = read_json(claimed)
        if not isinstance(batch, dict) or not batch.get("segments"):
            self.note(f"Bad batch file {claimed.name}; dropped")
            claimed.unlink(missing_ok=True)
            return
        bid = batch["id"]
        segments = batch["segments"]
        partial = self.paths.done / f"{bid}.partial"
        partial.mkdir(parents=True, exist_ok=True)
        lines = sorted({s["strref"] for s in segments})
        with self.lock:
            self.batch = {"id": bid, "segments": len(segments), "done": 0, "lines": len(lines), "started": time.time()}
        self.note(f"Batch {bid}: {len(lines)} lines, {len(segments)} segments")
        self.load_model()
        self.phase = "working"
        results = []
        left_per_line = {s: sum(1 for x in segments if x["strref"] == s) for s in lines}
        for n, seg in enumerate(segments, 1):
            if self.master_gone():
                self.serving, self.quit = False, True
            if not self.serving or self.quit:
                os.replace(claimed, self.paths.inbox / claimed.name)
                shutil.rmtree(partial, ignore_errors=True)
                self.note(f"Stopped: batch {bid} handed back ({n - 1}/{len(segments)} were done)")
                with self.lock:
                    self.batch, self.current = {}, {}
                return
            with self.lock:
                self.current = {"strref": seg["strref"], "index": seg["index"], "resref": seg.get("resref", ""),
                                "speaker": seg.get("speaker", ""), "text": seg["text"], "n": n,
                                "of": len(segments), "started": time.time()}
            t0 = time.time()
            result = {"id": seg["id"], "strref": seg["strref"], "index": seg["index"], "text": seg["text"],
                      "voice": seg["voice"]}
            try:
                from libs.drama3_takes import speak_segment
                name = self.voice_name(seg["voice"], batch["voices"][seg["voice"]])
                take, attempts = speak_segment(self.engine, seg["strref"], seg["index"], name, seg["text"])
                file = f"{seg['id']}.wav"
                take.save(partial / file)
                result.update(file=file, seed=take.seed, stop=take.stop, seconds=round(take.seconds, 3),
                              attempts=attempts)
                with self.lock:
                    self.counts["segments"] += 1
                    self.counts["audio_seconds"] += take.seconds
                    self.counts["retries"] += max(0, sum(1 for a in attempts if not a.startswith("chunk")) - 1)
                flag = "" if attempts[-1] == "ok" else f"  ⚠ {attempts[-1]}"
                self.note(f"✅ {seg.get('speaker', '')} {seg['strref']}#{seg['index']} "
                          f"{take.seconds:.1f}s in {time.time() - t0:.0f}s{flag}")
            except Exception as e:  # noqa: BLE001 - one bad segment must not stop the batch
                result["error"] = str(e)[:500]
                with self.lock:
                    self.counts["errors"] += 1
                self.note(f"❌ {seg['strref']}#{seg['index']}: {e}")
                if self.engine is not None and self.engine.proc.poll() is not None:
                    self.engine = None     # speech.exe died: reload for the next segment
            results.append(result)
            self.done_times.append(time.time())
            self.segment_seconds.append(time.time() - t0)
            left_per_line[seg["strref"]] -= 1
            with self.lock:
                self.batch["done"] = n
                if left_per_line[seg["strref"]] == 0:
                    self.counts["lines"] += 1
        write_json(partial / "result.json", {"id": bid, "node": self.name, "host": socket.gethostname(),
                                             "device": self.device, "started": self.batch.get("started"),
                                             "finished": time.time(), "segments": results})
        os.replace(partial, self.paths.done / bid)
        claimed.unlink(missing_ok=True)
        with self.lock:
            self.counts["batches"] += 1
            self.batch, self.current = {}, {}
        self.note(f"Batch {bid} done")

    def master_gone(self) -> bool:
        if self.master_state is None:
            return False
        state = read_json(self.master_state) or {}
        return not state.get("running") or time.time() - float(state.get("time", 0)) > MASTER_TIMEOUT

    def loop(self) -> None:
        self.recover()
        self.note(f"Worker {self.name} ready. GPUs: {', '.join(self.gpus) or 'none found'}")
        if not Path(self.tts.SPEECH_EXE).exists() or not Path(self.tts.DRAMA3_MODEL).exists():
            self.error = f"Engine missing: {self.tts.SPEECH_EXE} / {self.tts.DRAMA3_MODEL}. Run Update farm PCs."
            self.note(self.error)
        started_wait = time.time()
        while not self.quit:
            try:
                if self.master_state is not None and time.time() - started_wait > MASTER_TIMEOUT \
                        and self.master_gone():
                    self.note("Master stopped; exiting")
                    break
                if not self.serving:
                    self.phase = "paused"
                    time.sleep(POLL_SECONDS / 2)
                    continue
                if time.time() < self.retry_at:
                    self.phase = "error"
                    time.sleep(POLL_SECONDS)
                    continue
                claimed = self.claim()
                if claimed is None:
                    self.phase = "idle"
                    idle_minutes = float(self.config.get("idle_unload_minutes") or 0)
                    if self.engine is not None and idle_minutes and time.time() - self.idle_since > idle_minutes * 60:
                        self.unload_model()
                    time.sleep(POLL_SECONDS)
                    continue
                self.error = ""
                self.run_batch(claimed)
                self.idle_since = time.time()
            except Exception as e:  # noqa: BLE001 - keep serving; the window shows the error
                self.error = explain_error(str(e))[:700]
                self.phase = "error"
                self.note(f"❌ {e}")
                for path in self.paths.working.glob("*.json"):
                    os.replace(path, self.paths.inbox / path.name)   # the master gives these to another PC
                self.unload_model()
                self.retry_at = time.time() + ERROR_BACKOFF
                self.note(f"Retrying in {ERROR_BACKOFF / 60:.0f} min (Stop/Start serving retries now)")
        self.quit = True
        self.unload_model()


def explain_error(message: str) -> str:
    """Add what to do to the errors a farm PC user can fix."""
    if "out_of_memory" in message and "on CPU" in message:
        return (message + " | Not enough free RAM: Drama 3 needs ~16 GB of RAM for the layers that do not fit in "
                "this GPU. Close big programs (games, browsers), and set the Windows page file to "
                "'System managed' (System > About > Advanced system settings > Performance > Advanced > Virtual "
                "memory). Then click Stop serving / Start serving to retry.")
    if "out_of_memory" in message:
        return message + " | Not enough free GPU memory: close games or other GPU programs, then retry."
    return message


# ==================== WINDOW ====================

def run_window(worker: FarmWorker) -> None:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(f"PST Farm Worker — {worker.name}")
    root.geometry("760x560")
    root.minsize(520, 420)
    big = ("Segoe UI", 15, "bold")
    status_var, gpu_var, batch_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
    line_var, stats_var, error_var = tk.StringVar(), tk.StringVar(), tk.StringVar()

    top = ttk.Frame(root, padding=10)
    top.pack(fill="x")
    status_lbl = tk.Label(top, textvariable=status_var, font=big, anchor="w", padx=10, pady=6)
    status_lbl.pack(fill="x")
    ttk.Label(top, textvariable=gpu_var, foreground="#555").pack(fill="x", pady=(4, 0))
    buttons = ttk.Frame(top)
    buttons.pack(fill="x", pady=8)
    serve_btn = ttk.Button(buttons, width=18)
    serve_btn.pack(side="left")

    def toggle():
        worker.serving = not worker.serving
        worker.retry_at = 0.0
        worker.note("Serving" if worker.serving else "Stop requested (finishing the current segment)")

    serve_btn.configure(command=toggle)

    def unload():
        if worker.phase == "working":
            worker.note("Stop serving first; the model is busy")
            return
        threading.Thread(target=worker.unload_model, daemon=True).start()

    ttk.Button(buttons, text="Unload model", command=unload).pack(side="left", padx=6)
    ttk.Button(buttons, text="Open folder", command=lambda: os.startfile(worker.paths.root)).pack(side="left")

    mid = ttk.LabelFrame(root, text="Now speaking", padding=10)
    mid.pack(fill="x", padx=10)
    ttk.Label(mid, textvariable=batch_var).pack(fill="x")
    bar = ttk.Progressbar(mid, mode="determinate")
    bar.pack(fill="x", pady=4)
    ttk.Label(mid, textvariable=line_var, wraplength=700, justify="left").pack(fill="x")
    ttk.Label(root, textvariable=stats_var, padding=(10, 6)).pack(fill="x")
    ttk.Label(root, textvariable=error_var, foreground="#b91c1c", wraplength=720, padding=(10, 0)).pack(fill="x")
    log_box = tk.Text(root, height=12, wrap="none", font=("Consolas", 9))
    log_box.pack(fill="both", expand=True, padx=10, pady=(4, 10))
    shown = {"n": 0}
    colors = {"working": ("#166534", "#dcfce7"), "idle": ("#1e3a8a", "#dbeafe"), "loading": ("#92400e", "#fef3c7"),
              "paused": ("#3f3f46", "#e4e4e7"), "error": ("#991b1b", "#fee2e2"), "starting": ("#3f3f46", "#e4e4e7")}

    def refresh():
        s = worker.snapshot()
        phase = s["phase"]
        label = {"working": "Serving — speaking lines", "idle": "Serving — waiting for work from the master",
                 "loading": "Serving — loading Drama 3…", "paused": "Not serving",
                 "error": "Error — not taking work (see below)", "starting": "Starting…"}.get(phase, phase)
        if not s["serving"] and phase == "working":
            label = "Stopping after this segment…"
        fg, bg = colors.get(phase, ("#000", "#eee"))
        status_lbl.configure(fg=fg, bg=bg)
        status_var.set(label)
        serve_btn.configure(text="Stop serving" if s["serving"] else "Start serving")
        model = f"loaded on {s['device']}" if s["model_loaded"] else "not loaded"
        gpu_var.set(f"GPU: {', '.join(s['gpus']) or '?'}  ·  model {model}  ·  queued batches: {s['inbox']}")
        b, c = s["batch"], s["current"]
        if b:
            batch_var.set(f"Batch {b['id']}  ·  segment {c.get('n', b['done'])}/{b['segments']}  ·  {b['lines']} lines")
            bar.configure(maximum=max(1, b["segments"]), value=b["done"])
        else:
            batch_var.set("No batch")
            bar.configure(value=0)
        if c:
            line_var.set(f"{c.get('speaker', '')} {c.get('strref')}#{c.get('index')} ({c.get('resref', '')}) "
                         f"— {time.time() - c.get('started', time.time()):.0f}s\n{c.get('text', '')}")
        else:
            line_var.set("")
        k = s["counts"]
        stats_var.set(f"This session: {k['lines']} lines, {k['segments']} segments, "
                      f"{k['audio_seconds'] / 60:.1f} min of audio, {k['retries']} retries, {k['errors']} errors  ·  "
                      f"{s['segments_per_minute']:.1f} segments/min, {s['seconds_per_segment']:.0f} s each")
        error_var.set(s["error"])
        with worker.lock:
            lines = list(worker.log)
        total = len(lines)
        if total != shown["n"] or total == worker.log.maxlen:
            log_box.delete("1.0", "end")
            log_box.insert("end", "\n".join(lines[-200:]))
            log_box.see("end")
            shown["n"] = total
        if worker.quit and not worker_thread.is_alive():
            root.destroy()
            return
        root.after(700, refresh)

    def on_close():
        worker.serving = False
        worker.quit = True
        status_var.set("Closing (finishing the current segment)…")
        root.after(200, wait_close)

    def wait_close():
        if worker_thread.is_alive():
            root.after(300, wait_close)
        else:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    worker_thread = threading.Thread(target=worker.loop, daemon=True)
    worker_thread.start()
    threading.Thread(target=worker.heartbeat_loop, daemon=True).start()
    refresh()
    root.mainloop()
    worker.quit = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=None, help="pst_farm folder (default: parent of app/)")
    parser.add_argument("--headless", action="store_true", help="No window (the master runs This PC like this)")
    parser.add_argument("--serve", action="store_true", help="Start serving immediately")
    parser.add_argument("--name", help="Name shown on the master")
    parser.add_argument("--master-state", type=Path, help="Exit when this master state file stops updating")
    args = parser.parse_args()

    paths = FarmPaths((args.root or default_root()).resolve())
    paths.make_dirs()
    (paths.root / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[
        logging.FileHandler(paths.root / "logs" / "farm_worker.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)])
    config = load_config(paths)
    if args.name:
        config["name"] = args.name
    if args.serve:
        config["serve_on_start"] = True
    configure_engine(paths, config)
    master_state = args.master_state.resolve() if args.master_state else None
    os.chdir(paths.root)
    worker = FarmWorker(paths, config, master_state)
    if args.headless:
        threading.Thread(target=worker.heartbeat_loop, daemon=True).start()
        try:
            worker.loop()
        except KeyboardInterrupt:
            worker.quit = True
        worker.quit = True
        time.sleep(HEARTBEAT_SECONDS + 0.5)
        return
    run_window(worker)


if __name__ == "__main__":
    main()
