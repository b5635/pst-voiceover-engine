#!/usr/bin/env python3
"""
PST Farm: spread Fish Audio Drama 3 generation over several PCs.

This PC (the master) plans, directs and stitches; farm PCs only speak. They
are reached through their shared folders (pst-farm.json lists them), so no
server or open port is needed:

    python pst_farm.py sync              # "Update farm PCs": copy worker code, speech.cpp and the model
    python pst_farm.py run --stale       # "Farm generate": dispatch missing/stale lines, pull takes back
    python pst_farm.py run --no-local    # farm PCs only (keep this GPU free)
    python pst_farm.py status            # who is serving

How a line travels:

    1. The master picks the lines Generate missing would speak (pst_fish.select_todo),
       builds their Drama 3 texts and voice files exactly as pst_fish does.
    2. It writes a lock file per line (pst_revoice/farm/locks/<strref>.lock) and a
       batch of a few lines into that PC's queue/inbox, with the voice files they need.
    3. The PC's worker (pst_farm_worker.py) claims the batch, speaks each segment with
       the same seeds and take checks, and writes the WAVs to queue/done.
    4. The master copies the takes into pst_revoice/audio_fish/segments, stitches the
       line, writes generation.json, deletes the batch and the locks.

A take is only used if its text still matches what the line would speak now,
so editing a line while it is out on a PC never puts stale words in the game.
A PC that stops serving hands its waiting batches back; one that stays silent
for DEAD_SECONDS loses its lines to the others. Stop: Ctrl+C, or create
pst_revoice/farm/stop.flag (the GUI's Stop job does that).
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

from libs.farm import (
    DEAD_SECONDS,
    FARM_DIRNAME,
    PROTOCOL_VERSION,
    FarmPaths,
    heartbeat_age,
    is_online,
    new_batch_id,
    read_json,
    write_json,
)
from libs.tts_drama3 import DRAMA3_MODEL, SPEECH_EXE, Take
from libs.utils import setup_logging

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logger = setup_logging(Path(__file__).stem)

# ==================== CONFIGURATION ====================
CONFIG_PATH = Path(os.environ.get("PST_FARM_CONFIG", "pst-farm.json"))
FARM_DIR = Path("pst_revoice/farm")
LOCKS_DIR = FARM_DIR / "locks"
STATE_PATH = FARM_DIR / "state.json"          # dispatcher state, read by pst_gui.py
STOP_FLAG = FARM_DIR / "stop.flag"
LOCAL_ROOT = FARM_DIR / "local"               # This PC's worker queue
LOCAL_NAME = "This PC"
APP_FILES = ["pst_farm_worker.py", "libs/farm.py", "libs/tts_drama3.py", "libs/drama3_takes.py"]
DEFAULT_CONFIG = {
    "nodes": [],                  # [{"name": "Maymay", "share": "\\\\Maymay\\vve", "enabled": true}]
    "batch_lines": 4,             # lines per batch (a batch is one voice where possible)
    "batches_ahead": 2,           # batches queued per PC so it never waits for the master
    "use_this_pc": True,
}
LOOP_SECONDS = 3.0
PROBE_TIMEOUT = 4.0               # a share that does not answer this fast counts as offline this round
RESCAN_CHECK_SECONDS = 20.0       # how often input files are checked for edits
MAX_LINE_FAILURES = 2             # a line whose takes fail this often is left for the next run
RATE_WINDOW = 1800.0              # seconds of finished lines the lines/minute figure uses
COPY_CHUNK = 8 * 1024 * 1024
# =======================================================


def load_config() -> Dict:
    """pst-farm.json merged over the defaults."""
    data = read_json(CONFIG_PATH)
    return {**DEFAULT_CONFIG, **(data if isinstance(data, dict) else {})}


def node_root(share: str) -> Path:
    return Path(share) / FARM_DIRNAME


# ==================== SYNC ("Update farm PCs") ====================

def _needs_copy(src: Path, dst: Path) -> bool:
    try:
        d = dst.stat()
    except OSError:
        return True
    s = src.stat()
    return s.st_size != d.st_size or s.st_mtime > d.st_mtime + 2


def copy_file(src: Path, dst: Path, label: str = "") -> bool:
    """Copy src to dst if it changed (size or newer), with progress for big files. True if copied."""
    if not _needs_copy(src, dst):
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    size = src.stat().st_size
    tmp = dst.with_name(dst.name + ".copying")
    if size < 64 * 1024 * 1024:
        shutil.copy2(src, tmp)
    else:
        done, next_pct, t0 = 0, 5, time.time()
        with src.open("rb") as fi, tmp.open("wb") as fo:
            while True:
                chunk = fi.read(COPY_CHUNK)
                if not chunk:
                    break
                fo.write(chunk)
                done += len(chunk)
                pct = done * 100 // size
                if pct >= next_pct:
                    speed = done / max(0.1, time.time() - t0) / 2**20
                    eta = (size - done) / max(1, speed * 2**20)
                    print(f"📦 {label or src.name}: {pct}% ({done / 2**30:.1f}/{size / 2**30:.1f} GB, "
                          f"{speed:.0f} MB/s, {eta / 60:.0f} min left)", flush=True)
                    next_pct = pct + 5
        shutil.copystat(src, tmp)
    os.replace(tmp, dst)
    return True


def worker_scripts() -> Dict[str, str]:
    """The .bat files and README written to a farm PC's pst_farm folder."""
    find_python = (
        'set "PY="\r\n'
        'py -3 -c "import sys" >nul 2>nul && set "PY=py -3"\r\n'
        'if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"\r\n'
    )
    run = (
        "@echo off\r\n"
        "rem PST Farm Worker: speaks Drama 3 lines for the master PC. Leave it open while you want to help.\r\n"
        "title PST Farm Worker\r\n"
        'pushd "%~dp0"\r\n' + find_python +
        "if not defined PY (\r\n"
        "  echo Python is not installed. Run install_worker.bat first.\r\n"
        "  pause\r\n  exit /b 1\r\n)\r\n"
        "%PY% app\\pst_farm_worker.py %*\r\n"
        "if errorlevel 1 pause\r\n"
        "popd\r\n"
    )
    install = (
        "@echo off\r\n"
        "rem PST Farm Worker setup: Python + numpy, optional local copy of the model, desktop shortcut.\r\n"
        "rem Safe to run again after every Update farm PCs.\r\n"
        "title PST Farm Worker setup\r\n"
        'pushd "%~dp0"\r\n' + find_python +
        "if not defined PY (\r\n"
        "  echo Python not found. Installing Python 3.13 with winget...\r\n"
        "  winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements\r\n"
        "  echo.\r\n  echo Python installed. Close this window and run install_worker.bat again.\r\n"
        "  pause\r\n  exit /b 1\r\n)\r\n"
        "echo Using %PY%\r\n"
        "%PY% -m pip install --upgrade --disable-pip-version-check numpy\r\n"
        '%PY% -c "import tkinter, numpy; print(\'Python, tkinter and numpy OK\')"\r\n'
        "if errorlevel 1 (\r\n  echo Setup failed, see above.\r\n  pause\r\n  exit /b 1\r\n)\r\n"
        "echo.\r\n"
        "echo The Drama 3 model loads in ~20 s from an SSD and several minutes from a hard disk.\r\n"
        "echo If this folder is on a hard disk, copy the model to C: (21 GB).\r\n"
        "choice /c YN /m \"Copy the model to C:\\fish-drama3\\models\"\r\n"
        "if errorlevel 2 goto shortcut\r\n"
        'robocopy "engine\\models" "C:\\fish-drama3\\models" *.gguf /NJH /NJS /NDL\r\n'
        "%PY% -c \"import json,glob,pathlib;p=pathlib.Path('worker-config.json');"
        "c=json.loads(p.read_text()) if p.exists() else {};"
        "c['model']=sorted(glob.glob(r'C:\\fish-drama3\\models\\*.gguf'))[0];"
        "p.write_text(json.dumps(c,indent=1));print('Model:',c['model'])\"\r\n"
        ":shortcut\r\n"
        "powershell -NoProfile -Command \"$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
        "[Environment]::GetFolderPath('Desktop')+'\\PST Farm Worker.lnk');"
        "$s.TargetPath='%~dp0run_worker.bat';$s.WorkingDirectory='%~dp0';$s.Save()\"\r\n"
        "echo.\r\necho Done. Start the worker with the \"PST Farm Worker\" desktop shortcut (run_worker.bat).\r\n"
        "pause\r\npopd\r\n"
    )
    readme = (
        "PST Farm Worker\r\n===============\r\n\r\n"
        "This folder is managed by the master PC (PST Revoice > Update farm PCs). It holds the worker\r\n"
        "code (app), speech.cpp and the Fish Audio Drama 3 model (engine), and the job queue (queue).\r\n\r\n"
        "1. Once (and after updates): run install_worker.bat on THIS PC, from its own disk (not over the network).\r\n"
        "2. To help: run run_worker.bat (or the desktop shortcut). The window shows what it is speaking.\r\n"
        "   Close it or click Stop serving when you need the PC; unfinished work goes back to the master.\r\n\r\n"
        "worker-config.json is this PC's own settings (device, threads, model path, idle unload) and is\r\n"
        "never overwritten by updates.\r\n\r\nBuilt with Fish Audio.\r\n"
    )
    return {"run_worker.bat": run, "install_worker.bat": install, "README.txt": readme}


def sync_node(name: str, share: str, with_model: bool = True) -> bool:
    """Copy the bare minimum a farm PC needs. True on success."""
    root = node_root(share)
    paths = FarmPaths(root)
    print(f"📦 {name}: updating {root}", flush=True)
    try:
        paths.make_dirs()
    except OSError as e:
        print(f"❌ {name}: cannot write {root}: {e}", flush=True)
        return False
    copied = 0
    for rel in APP_FILES:
        copied += copy_file(Path(rel), paths.app / rel)
    for fname, text in worker_scripts().items():
        target = root / fname
        if not target.exists() or target.read_text(encoding="utf-8", errors="replace") != text.replace("\r\n", "\n"):
            target.write_bytes(text.encode("utf-8"))
            copied += 1
    engine_files = [(SPEECH_EXE.parent / n, paths.speech_exe.parent / n) for n in ("speech.exe", "speech.dll")]
    if with_model:
        engine_files.append((DRAMA3_MODEL, paths.models / DRAMA3_MODEL.name))
        for extra in ("LICENSE.md", "NOTICE"):
            if (DRAMA3_MODEL.parent / extra).exists():
                engine_files.append((DRAMA3_MODEL.parent / extra, paths.models / extra))
    ok = True
    for src, dst in engine_files:
        if not src.exists():
            print(f"⚠️ {name}: {src} missing on this PC; skipped", flush=True)
            continue
        try:
            if copy_file(src, dst, f"{name} {src.name}"):
                copied += 1
                print(f"📦 {name}: copied {src.name}", flush=True)
        except PermissionError:
            ok = False
            print(f"❌ {name}: {dst.name} is in use. Stop the worker on {name}, then Update again.", flush=True)
        except OSError as e:
            ok = False
            print(f"❌ {name}: copying {src.name} failed: {e}", flush=True)
    write_json(root / "version.json", {"protocol": PROTOCOL_VERSION, "updated": time.time(),
                                        "from": socket.gethostname(), "model": DRAMA3_MODEL.name})
    print(f"📦 {name}: {'up to date' if not copied else f'{copied} files updated'}"
          f"{'' if ok else ' (with errors)'}", flush=True)
    return ok


def cmd_sync(args) -> int:
    config = load_config()
    nodes = [n for n in config["nodes"] if n.get("enabled", True) and (not args.node or n["name"] in args.node)]
    if not nodes:
        print("❌ No farm PCs in pst-farm.json", flush=True)
        return 1
    ok = all([sync_node(n["name"], n["share"], not args.no_model) for n in nodes])
    print("📦 Update finished. On each farm PC: run install_worker.bat once, then run_worker.bat.", flush=True)
    return 0 if ok else 1


# ==================== NODES ====================

@dataclass
class Node:
    """One PC that can speak lines (a farm PC, or This PC)."""
    name: str
    paths: FarmPaths
    local: bool = False
    status: Optional[Dict] = None
    reachable: bool = False
    done_batches: List[str] = field(default_factory=list)
    inbox: Set[str] = field(default_factory=set)
    working: Set[str] = field(default_factory=set)
    probed_at: float = 0.0
    last_seen: float = 0.0
    last_key: str = ""
    lines_done: int = 0
    done_times: Deque[float] = field(default_factory=deque)
    _probe: Optional[threading.Thread] = None
    _probe_started: float = 0.0

    def online(self) -> bool:
        return self.reachable and is_online(self.status)

    def serving(self) -> bool:
        return self.online() and bool((self.status or {}).get("serving"))

    def failing(self) -> bool:
        """Online but in an error state (e.g. could not load the model): give it no work."""
        return self.online() and (self.status or {}).get("phase") == "error"

    def probe(self) -> None:
        """Refresh status, inbox and done list in the background (an offline share can block for ~20 s)."""
        if self._probe is not None and self._probe.is_alive():
            if time.time() - self._probe_started > PROBE_TIMEOUT:
                self.reachable = False
            return

        def run():
            try:
                status = read_json(self.paths.status)
                done = sorted(p.name for p in self.paths.done.iterdir()
                              if p.is_dir() and not p.name.endswith(".partial"))
                inbox = {p.stem for p in self.paths.inbox.glob("*.json")}
                working = {p.stem for p in self.paths.working.glob("*.json")}
                self.status, self.done_batches, self.inbox, self.working = status, done, inbox, working
                self.probed_at = self._probe_started
                self.reachable = True
                if is_online(status):
                    self.last_seen = time.time()
            except OSError:
                self.reachable = False

        self._probe_started = time.time()
        self._probe = threading.Thread(target=run, daemon=True)
        self._probe.start()
        self._probe.join(PROBE_TIMEOUT)

    def rate(self) -> float:
        """Lines per minute this run (recent window)."""
        return _rate(self.done_times)


def _rate(times: Deque[float]) -> float:
    now = time.time()
    while times and now - times[0] > RATE_WINDOW:
        times.popleft()
    if len(times) < 2:
        return 0.0
    return len(times) / max(120.0, now - times[0]) * 60


def make_nodes(config: Dict, use_local: bool) -> List[Node]:
    nodes = []
    if use_local:
        nodes.append(Node(LOCAL_NAME, FarmPaths(LOCAL_ROOT.resolve()), local=True))
    for n in config["nodes"]:
        if n.get("enabled", True):
            nodes.append(Node(n["name"], FarmPaths(node_root(n["share"]))))
    return nodes


# ==================== LOCKS ====================

def lock_path(strref: int) -> Path:
    return LOCKS_DIR / f"{strref}.lock"


def take_lock(strref: int, node: str, batch: str) -> bool:
    """Create the line's lock file; False if another PC already holds it."""
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path(strref).open("x", encoding="utf-8") as f:
            json.dump({"strref": strref, "node": node, "batch": batch, "at": time.time(),
                       "master": socket.gethostname()}, f)
        return True
    except FileExistsError:
        return False


def read_locks() -> Dict[int, Dict]:
    out = {}
    for p in LOCKS_DIR.glob("*.lock"):
        data = read_json(p)
        if isinstance(data, dict) and "strref" in data:
            out[int(data["strref"])] = data
        else:
            p.unlink(missing_ok=True)
    return out


def release_lock(strref: int, batch: Optional[str] = None) -> None:
    """Delete a line's lock (only if it belongs to `batch`, when given)."""
    path = lock_path(strref)
    if batch is not None:
        data = read_json(path) or {}
        if data.get("batch") != batch:
            return
    path.unlink(missing_ok=True)


# ==================== DISPATCHER ====================

class Dispatcher:
    """Keeps every serving PC busy with batches, and pulls their takes back."""

    def __init__(self, args, config: Dict):
        import pst_fish
        from pst_revoice import PLAN_PATH, read_plan
        self.fish = pst_fish
        self.read_plan, self.plan_path = read_plan, PLAN_PATH
        self.args = args
        self.config = config
        self.batch_lines = max(1, int(args.batch_lines or config["batch_lines"]))
        self.ahead = max(1, int(config["batches_ahead"]))
        self.use_local = config["use_this_pc"] if args.local is None else args.local
        self.nodes = make_nodes(config, self.use_local)
        self.out_dir = pst_fish.OUT_DIR
        self.segment_dir = self.out_dir / "segments"
        self.directions_path = pst_fish.DIRECTIONS_PATH
        self.rows: Dict[int, object] = {}          # strref -> PlanRow still to speak
        self.jobs: Dict[int, List] = {}            # strref -> its pst_fish.Job list
        self.voice_files: Dict[str, Path] = {}
        self.order: List[int] = []                 # pending strrefs, grouped by voice
        self.failures: Dict[int, int] = {}
        self.given_up: Set[int] = set()
        self.done_times: Deque[float] = deque()
        self.done_run = 0
        self.started = time.time()
        self.total_plan = 0
        self.voiced = 0
        self.inputs_sig: Tuple = ()
        self.last_rescan_check = 0.0
        self.local_proc: Optional[subprocess.Popen] = None
        self.stopping = False

    # ---------- planning ----------

    def _inputs_signature(self) -> Tuple:
        from pst_revoice import CAST_OVERRIDES_PATH, LINE_OVERRIDES_PATH, PRONUNCIATION_PATH
        files = [self.plan_path, self.directions_path, LINE_OVERRIDES_PATH, PRONUNCIATION_PATH,
                 CAST_OVERRIDES_PATH, Path(self.fish.PATCHER_CONFIG_PATH)]
        return tuple(p.stat().st_mtime if p.exists() else 0 for p in files)

    def scan(self, first: bool = False) -> None:
        """Work out which lines still need speaking, their Drama 3 jobs and voice files."""
        self.inputs_sig = self._inputs_signature()
        plan = [p for p in self.read_plan(self.plan_path) if p.action == "generate"]
        rows = plan
        if self.args.speaker:
            rows = [p for p in rows if p.speaker in self.args.speaker]
        if self.args.strref_file:
            wanted = {int(x) for x in Path(self.args.strref_file).read_text(encoding="utf-8").split()}
            rows = [p for p in rows if p.strref in wanted]
        todo = self.fish.select_todo(rows, self.out_dir, self.directions_path, self.args.limit, self.args.force,
                                     "multi", self.args.stale, quiet=not first)
        directions = self.fish.load_directions(self.directions_path)
        jobs, refs = self.fish.build_jobs(todo, directions, "multi") if todo else ([], {})
        self.voice_files = self.fish.build_voice_files(refs)
        by_row: Dict[int, List] = {}
        for job in jobs:
            by_row.setdefault(job.row.strref, []).append(job)
        self.rows = {p.strref: p for p in todo if p.strref in by_row}
        self.jobs = by_row
        self.total_plan = len(plan)
        self.voiced = sum(1 for p in plan if (self.out_dir / f"{p.resref}.WAV").exists())
        # Lines that only keep original game audio need no TTS: stitch them right here
        original_only = [s for s, js in by_row.items() if all(j.original_path is not None for j in js)]
        if original_only:
            self._stitch(original_only, None)

        def first_key(s: int) -> str:
            return next((j.ref_key for j in self.jobs[s] if j.original_path is None), "")
        self.order = sorted((s for s in self.rows if s not in self.given_up), key=lambda s: (first_key(s), s))
        tts = sum(1 for js in self.jobs.values() for j in js if j.original_path is None)
        logger.info(f"📋 {len(self.rows)} lines to speak ({tts} Drama 3 segments, {len(self.voice_files)} voices); "
                    f"{self.voiced}/{self.total_plan} lines voiced")

    def _stitch(self, strrefs: List[int], node: Optional[Node]) -> List[int]:
        """Stitch lines whose takes are all in; returns the strrefs actually written."""
        def mtime(s: int) -> float:
            wav = self.out_dir / f"{self.rows[s].resref}.WAV"
            return wav.stat().st_mtime if wav.exists() else 0.0
        before = {s: mtime(s) for s in strrefs}
        self.fish.stitch_lines([self.rows[s] for s in strrefs], [j for s in strrefs for j in self.jobs[s]],
                               self.out_dir, self.directions_path, "multi", self.segment_dir)
        written = [s for s in strrefs if mtime(s) != before[s]]
        now = time.time()
        for s in written:
            if node is not None:
                logger.info(f"✅ {node.name}: {self.rows[s].speaker} {s} -> {self.rows[s].resref}.WAV")
                node.lines_done += 1
                node.done_times.append(now)
            self.rows.pop(s, None)
            self.jobs.pop(s, None)
            if s in self.order:
                self.order.remove(s)
            self.done_times.append(now)
        self.done_run += len(written)
        self.voiced += sum(1 for s in written if before[s] == 0.0)
        return written

    # ---------- collecting ----------

    def collect(self, node: Node) -> None:
        """Pull finished batches back from a PC, stitch their lines, release their locks."""
        for bid in list(node.done_batches):
            folder = node.paths.done / bid
            result = read_json(folder / "result.json")
            if not isinstance(result, dict):
                continue
            by_line: Dict[int, List[Dict]] = {}
            for seg in result.get("segments", []):
                by_line.setdefault(int(seg["strref"]), []).append(seg)
            ready, failed, stale = [], [], []
            for strref, segs in by_line.items():
                jobs = self.jobs.get(strref)
                if not jobs:
                    stale.append(strref)       # finished elsewhere meanwhile, or no longer needed
                    continue
                tts = {j.index: j for j in jobs if j.original_path is None}
                got = {int(s["index"]): s for s in segs}
                if set(got) != set(tts) or any(got[i].get("text") != tts[i].text
                                                or got[i].get("voice") != tts[i].ref_key for i in tts):
                    stale.append(strref)       # edited while it was out: speak it again
                    continue
                if any(s.get("error") or not s.get("file") for s in segs):
                    failed.append(strref)
                    continue
                for i, job in tts.items():
                    seg = got[i]
                    dst = self.segment_dir / f"{job.row.resref}_{i}.wav"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(folder / seg["file"], dst)
                    job.take = load_take(dst, int(seg.get("seed") or 0), seg.get("stop", "complete"))
                    job.attempts = list(seg.get("attempts") or ["ok"])
                ready.append(strref)
            if ready:
                written = self._stitch(ready, node)
                failed += [s for s in ready if s not in written]
            for s in failed:
                self.failures[s] = self.failures.get(s, 0) + 1
                err = next((x.get("error") for x in by_line[s] if x.get("error")), "no take")
                if self.failures[s] >= MAX_LINE_FAILURES:
                    self.given_up.add(s)
                    logger.warning(f"⚠️ {node.name}: {s} failed {self.failures[s]}x ({err}); left for the next run")
                else:
                    logger.warning(f"⚠️ {node.name}: {s} failed ({err}); will retry")
            for s in stale:
                if s in self.jobs:
                    logger.info(f"   {node.name}: {s} changed while it was being spoken; queued again")
            for s in by_line:
                release_lock(s, bid)
                if s in self.jobs and s not in self.order and s not in self.given_up:
                    self.order.append(s)
            shutil.rmtree(folder, ignore_errors=True)
            node.done_batches.remove(bid)

    # ---------- reclaiming ----------

    def reclaim(self, locks: Dict[int, Dict]) -> None:
        """Take lines back from PCs that stopped serving or went silent."""
        known = {n.name: n for n in self.nodes}
        for strref, lock in locks.items():
            node = known.get(lock.get("node"))
            batch = lock.get("batch")
            if node is None:
                release_lock(strref)
                continue
            silent = time.time() - max(node.last_seen, float(lock.get("at", 0))) > DEAD_SECONDS
            stopped = node.online() and not (node.status or {}).get("serving")
            local_gone = node.local and (self.local_proc is None or self.local_proc.poll() is not None) \
                and time.time() - float(lock.get("at", 0)) > 60
            failing = node.failing() and batch in node.inbox
            # The batch is nowhere on the PC (lost after an error or a restart): its lock is an orphan
            orphan = (node.online() and node.probed_at > float(lock.get("at", 0)) + 30
                      and batch not in node.inbox and batch not in node.working
                      and batch not in node.done_batches
                      and (node.status or {}).get("batch", {}).get("id") != batch)
            if not (silent or local_gone or failing or orphan or (stopped and batch in node.inbox)):
                continue
            if batch in node.inbox:
                try:
                    (node.paths.inbox / f"{batch}.json").unlink(missing_ok=True)
                    node.inbox.discard(batch)
                except OSError:
                    pass
            release_lock(strref)
            if strref in self.jobs and strref not in self.order:
                self.order.insert(0, strref)
            why = ("has an error" if failing else "lost its batch" if orphan
                   else "stopped serving" if stopped else "went silent")
            logger.info(f"   {node.name} {why}: line {strref} goes back to the queue")

    # ---------- dispatching ----------

    def _outstanding(self, node: Node, locks: Dict[int, Dict]) -> int:
        return len({lk["batch"] for lk in locks.values() if lk.get("node") == node.name}
                   - set(node.done_batches))

    def _pick(self, node: Node, locked: Set[int]) -> List[int]:
        """Up to batch_lines pending lines, keeping to one voice (the PC's last one when possible)."""
        free = [s for s in self.order if s not in locked and s in self.jobs]
        if not free:
            return []

        def key(s: int) -> str:
            return next((j.ref_key for j in self.jobs[s] if j.original_path is None), "")
        want = node.last_key if any(key(s) == node.last_key for s in free) else key(free[0])
        picked = [s for s in free if key(s) == want][: self.batch_lines]
        node.last_key = want
        return picked

    def dispatch(self, node: Node, locks: Dict[int, Dict]) -> None:
        while self._outstanding(node, locks) < self.ahead:
            locked = set(locks)
            strrefs = self._pick(node, locked)
            if not strrefs:
                return
            bid = new_batch_id(node.name)
            taken = [s for s in strrefs if take_lock(s, node.name, bid)]
            if not taken:
                return
            segments, voices = [], {}
            for s in taken:
                for job in self.jobs[s]:
                    if job.original_path is not None:
                        continue
                    segments.append({"id": f"{job.row.resref}_{job.index}", "strref": s, "index": job.index,
                                     "resref": job.row.resref, "speaker": job.row.speaker,
                                     "voice": job.ref_key, "text": job.text})
                    vf = self.voice_files[job.ref_key]
                    voices[job.ref_key] = {"file": vf.name}
            try:
                for key in voices:
                    copy_file(self.voice_files[key], node.paths.voices / self.voice_files[key].name)
                write_json(node.paths.inbox / f"{bid}.json",
                           {"id": bid, "protocol": PROTOCOL_VERSION, "created": time.time(),
                            "master": socket.gethostname(), "voices": voices, "segments": segments})
            except OSError as e:
                logger.warning(f"⚠️ {node.name}: could not send a batch ({e})")
                for s in taken:
                    release_lock(s, bid)
                return
            node.inbox.add(bid)
            for s in taken:
                locks[s] = {"strref": s, "node": node.name, "batch": bid, "at": time.time()}
                self.order.remove(s)
            speakers = sorted({self.rows[s].speaker for s in taken})
            logger.info(f"   → {node.name}: {len(taken)} lines ({', '.join(speakers)}), {len(segments)} segments")

    # ---------- This PC ----------

    def start_local(self) -> None:
        node = next((n for n in self.nodes if n.local), None)
        if node is None:
            return
        # This PC's queue restarts with the dispatcher: hand anything left from last time back
        node.paths.make_dirs()
        for folder in (node.paths.inbox, node.paths.working):
            for p in folder.glob("*.json"):
                p.unlink(missing_ok=True)
        for strref, lock in read_locks().items():
            if lock.get("node") == node.name:
                release_lock(strref)
        cmd = [sys.executable, str(Path("pst_farm_worker.py").resolve()), "--root", str(node.paths.root),
               "--headless", "--serve", "--name", LOCAL_NAME, "--master-state", str(STATE_PATH.resolve())]
        log = (node.paths.root / "worker_stdout.log").open("ab")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.local_proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        logger.info(f"🐟 This PC: worker started (device {os.environ.get('DRAMA3_DEVICE', 'default')})")

    # ---------- state ----------

    def write_state(self, locks: Dict[int, Dict], running: bool = True) -> None:
        in_flight = len(locks)
        remaining = len([s for s in self.jobs if s not in self.given_up])
        rate = _rate(self.done_times)
        if not rate:   # no finished lines yet: estimate from the PCs' segment speed
            seg_per_line = max(1.0, sum(1 for js in self.jobs.values() for j in js if j.original_path is None)
                               / max(1, len(self.jobs)))
            rate = sum(float((n.status or {}).get("segments_per_minute") or 0)
                       for n in self.nodes if n.serving()) / seg_per_line
        nodes = []
        for n in self.nodes:
            st = n.status or {}
            nodes.append({
                "name": n.name, "local": n.local, "reachable": n.reachable, "online": n.online(),
                "serving": n.serving(), "phase": st.get("phase", "offline") if n.online() else "offline",
                "device": st.get("device", ""), "gpus": st.get("gpus", []), "error": st.get("error", ""),
                "current": st.get("current", {}), "batch": st.get("batch", {}),
                "in_flight": sum(1 for lk in locks.values() if lk.get("node") == n.name),
                "lines_done": n.lines_done, "lines_per_minute": round(n.rate(), 2),
                "segments_per_minute": st.get("segments_per_minute", 0),
                "seen_ago": round(heartbeat_age(st), 1) if st else None,
            })
        write_json(STATE_PATH, {
            "running": running, "time": time.time(), "started": self.started, "pid": os.getpid(),
            "total_plan": self.total_plan, "voiced": self.voiced, "remaining": remaining,
            "pending": len(self.order), "in_flight": in_flight, "done_run": self.done_run,
            "given_up": len(self.given_up), "lines_per_minute": round(rate, 2),
            "eta_seconds": round(remaining / rate * 60) if rate else None,
            "serving": sum(1 for n in self.nodes if n.serving()), "nodes": nodes,
        })

    # ---------- main loop ----------

    def run(self) -> int:
        FARM_DIR.mkdir(parents=True, exist_ok=True)
        STOP_FLAG.unlink(missing_ok=True)
        self.scan(first=True)
        self.write_state(read_locks())
        if self.use_local:
            self.start_local()
        names = ", ".join(n.name for n in self.nodes) or "none"
        logger.info(f"🐟 Farm running with: {names}. Lines go to whichever PC is serving.")
        try:
            while not self.stopping:
                if STOP_FLAG.exists():
                    logger.info("Stop requested")
                    break
                for node in self.nodes:
                    node.probe()
                locks = read_locks()
                for node in self.nodes:
                    if node.reachable and node.done_batches:
                        self.collect(node)
                locks = read_locks()
                self.reclaim(locks)
                locks = read_locks()
                if time.time() - self.last_rescan_check > RESCAN_CHECK_SECONDS:
                    self.last_rescan_check = time.time()
                    if self._inputs_signature() != self.inputs_sig:
                        logger.info("📋 Plan, directions or line edits changed; rescanning")
                        self.scan()
                for node in self.nodes:
                    if node.serving() and not node.failing():
                        self.dispatch(node, locks)
                self.write_state(locks)
                if not self.order and not locks:
                    logger.info(f"✅ Farm finished: {self.done_run} lines this run"
                                + (f", {len(self.given_up)} failed lines left for next time" if self.given_up else ""))
                    break
                if self.args.once:
                    break
                time.sleep(LOOP_SECONDS)
        except KeyboardInterrupt:
            logger.info("Stopping (Ctrl+C)")
        finally:
            self.write_state(read_locks(), running=False)
            STOP_FLAG.unlink(missing_ok=True)
            if self.local_proc is not None:
                try:
                    self.local_proc.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    self.local_proc.kill()
        return 0


def load_take(path: Path, seed: int, stop: str) -> Take:
    with wave.open(str(path), "rb") as w:
        return Take(w.readframes(w.getnframes()), w.getframerate(), seed, stop)


def cmd_status(_args) -> int:
    config = load_config()
    for node in make_nodes(config, False):
        node.probe()
        st = node.status or {}
        state = "offline" if not node.online() else ("serving" if st.get("serving") else "not serving")
        cur = st.get("current") or {}
        print(f"{node.name:16} {state:12} {st.get('phase', ''):8} {', '.join(st.get('gpus', []))}  "
              f"{(str(cur.get('strref')) + ' ' + cur.get('speaker', '')) if cur else ''}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_sync = sub.add_parser("sync", help="Copy worker code, speech.cpp and the model to the farm PCs")
    p_sync.add_argument("--node", action="append", help="Only this PC (repeatable)")
    p_sync.add_argument("--no-model", action="store_true", help="Skip the 21 GB model")
    p_run = sub.add_parser("run", help="Dispatch lines to every serving PC until all are spoken")
    p_run.add_argument("--stale", action="store_true", help="Also regenerate stale WAVs (as Generate missing)")
    p_run.add_argument("--force", action="store_true", help="Regenerate even up-to-date lines")
    p_run.add_argument("--speaker", action="append", help="Only this character (repeatable)")
    p_run.add_argument("--strref-file", type=Path, help="Only the strrefs in this file")
    p_run.add_argument("--limit", type=int, default=0, help="At most N lines")
    p_run.add_argument("--batch-lines", type=int, help="Lines per batch (default from pst-farm.json)")
    local = p_run.add_mutually_exclusive_group()
    local.add_argument("--local", dest="local", action="store_true", default=None, help="Use This PC too")
    local.add_argument("--no-local", dest="local", action="store_false", help="Farm PCs only")
    p_run.add_argument("--once", action="store_true", help="One dispatch round (testing)")
    sub.add_parser("status", help="Show which farm PCs are serving")
    args = parser.parse_args()
    if args.cmd == "sync":
        sys.exit(cmd_sync(args))
    if args.cmd == "status":
        sys.exit(cmd_status(args))
    sys.exit(Dispatcher(args, load_config()).run())


if __name__ == "__main__":
    main()
