"""Farm lock files and change-only copying (pst_farm.py)."""

import os
import time
from pathlib import Path

import pst_farm


def test_lock_is_exclusive_and_released_only_by_its_batch(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pst_farm, "LOCKS_DIR", tmp_path / "locks")
    assert pst_farm.take_lock(7711, "Maymay", "b1")
    assert not pst_farm.take_lock(7711, "Desktop-WVAVS", "b2")
    pst_farm.release_lock(7711, "b2")              # another batch's release is ignored
    assert pst_farm.read_locks()[7711]["node"] == "Maymay"
    pst_farm.release_lock(7711, "b1")
    assert pst_farm.read_locks() == {}


def test_copy_file_only_when_changed(tmp_path: Path):
    src, dst = tmp_path / "a.txt", tmp_path / "out" / "a.txt"
    src.write_text("one")
    assert pst_farm.copy_file(src, dst)
    assert not pst_farm.copy_file(src, dst)
    src.write_text("three")
    os.utime(src, (time.time() + 10, time.time() + 10))
    assert pst_farm.copy_file(src, dst)
    assert dst.read_text() == "three"
