"""
Render farm protocol: the folder layout and file handshakes between the master
PC (pst_farm.py) and the PCs that speak Drama 3 for it (pst_farm_worker.py).

Everything goes through shared folders; there is no server or port. Each farm
PC's root (on the master: \\\\HOST\\vve\\pst_farm, on that PC: the same folder
on its own disk) holds:

    app/                    worker code, copied by "Update farm PCs"
    engine/speech/          speech.exe + speech.dll
    engine/models/          Drama 3 GGUF
    voices/                 .voice.gguf files the master sends
    queue/inbox/<id>.json   batches waiting (master writes, atomically)
    queue/working/<id>.json batch the worker claimed (worker renames from inbox)
    queue/done/<id>/        result.json + one WAV per segment (renamed from <id>.partial when complete)
    status.json             worker heartbeat (serving, model, current line, counters)
    worker-config.json      this PC's own settings (device, threads, model path); never overwritten

The master keeps one lock file per line it handed out
(pst_revoice/farm/locks/<strref>.lock) so no two PCs speak the same line.
Stdlib only: the worker imports this without the rest of the repo.
"""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

# ==================== CONFIGURATION ====================
FARM_DIRNAME = "pst_farm"         # folder under each PC's share
HEARTBEAT_SECONDS = 3.0           # worker writes status.json this often
ONLINE_SECONDS = 30.0             # a heartbeat older than this means the worker is not running
DEAD_SECONDS = 300.0              # the master takes a PC's lines back after this long without a heartbeat
PROTOCOL_VERSION = 1
# =======================================================


class FarmPaths:
    """The folders of one farm PC root."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.app = self.root / "app"
        self.engine = self.root / "engine"
        self.speech_exe = self.engine / "speech" / "speech.exe"
        self.models = self.engine / "models"
        self.voices = self.root / "voices"
        self.inbox = self.root / "queue" / "inbox"
        self.working = self.root / "queue" / "working"
        self.done = self.root / "queue" / "done"
        self.status = self.root / "status.json"
        self.config = self.root / "worker-config.json"

    def make_dirs(self) -> None:
        for d in (self.voices, self.inbox, self.working, self.done):
            d.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, data: Any) -> None:
    """Write JSON atomically (temp file + rename), so a reader on another PC never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:   # reader has it open (Windows); try again shortly
            time.sleep(0.2 * (attempt + 1))
    tmp.unlink(missing_ok=True)


def read_json(path: Path) -> Optional[Any]:
    """JSON contents, or None if missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def heartbeat_age(status: Optional[Dict]) -> float:
    """Seconds since a status.json was written (inf when there is none)."""
    if not status:
        return float("inf")
    return max(0.0, time.time() - float(status.get("time", 0)))


def is_online(status: Optional[Dict]) -> bool:
    """A worker that wrote its heartbeat recently."""
    return heartbeat_age(status) < ONLINE_SECONDS


def new_batch_id(node: str) -> str:
    """Sortable, unique batch id: time + node + random."""
    safe = "".join(c if c.isalnum() else "_" for c in node)[:20]
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}-{uuid.uuid4().hex[:6]}"
