#!/usr/bin/env python3
"""
PST examine / info-point lines (the ? cursor, ARE region type 1).

These are not dialog. They are first-person observations that float as overhead
text when you click an info point. Voice them with the Narrator profile (same
as unquoted "You see..." narration), never The Nameless One's spoken voice.

Talking objects (a dialog resref on the region) already go through DLG and are
skipped. Strrefs already in plan.csv are skipped on merge.

    python pst_infopoints.py                  # extract, verify, write infopoints.csv
    python pst_infopoints.py --merge --limit 10

GemRB plays the TLK sound on click, or VV<strref>.wav by name if the slot is
still empty. Do not STRING_SET these texts; sync_vve_voices.py only patches the
sound resref.
"""

from __future__ import annotations

import argparse
import csv
import struct
import sys
import zlib
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from libs.appconfig import cfg
from libs.utils import setup_logging
from pst_revoice import (
    FISH_AUDIO_DIR,
    NARRATOR,
    PLAN_PATH,
    PlanRow,
    read_plan,
    write_plan,
    write_plan_meta,
)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logger = setup_logging(Path(__file__).stem)

# ==================== CONFIGURATION ====================
INFOPOINTS_PATH = Path("pst_revoice/infopoints.csv")
BIF_CACHE = Path("pst_revoice/bifcache")
GEMRB_SYNC = Path(r"D:\gemrb\tools\sync_vve_voices.py")
COLUMNS = ["StrRef", "Speaker", "Action", "SoundResRef", "Text", "Voice"]
ARE_TYPE = 0x03F2
BCS_TYPE = 0x03EF
CRE_TYPE = 0x03F1
SRC_TYPE = 0x0803
INI_TYPE = 0x0802
EXT_BY_TYPE = {
    BCS_TYPE: "bcs", CRE_TYPE: "cre", ARE_TYPE: "are", SRC_TYPE: "src", INI_TYPE: "ini",
}
INFO_TYPE = 1
INVALID = 0xFFFFFFFF
REGION_SIZE = 0xC4
TEST_LIMIT = 10  # enough for GemRB sync to prove the path; full set is EXPECTED_COUNT

# Against classic PST + this plan.csv (dialog lines already skipped):
EXPECTED_COUNT = 318
EXPECTED_CHARS = 26266
EXPECTED_SUM = 6667938
EXPECTED_MIN = 828
EXPECTED_MAX = 68249
# =======================================================


def fallback_resref(strref: int) -> str:
    """VV<strref>; 8 characters or fewer for every PST info-point strref."""
    return f"VV{strref}"


def is_infopoint(row: PlanRow) -> bool:
    """True for ARE examine lines (Speaker=Narrator in the plan)."""
    return row.speaker == NARRATOR


# ==================== KEY / BIF (ARE only) ====================

def _resref(raw: bytes) -> str:
    text = raw.split(b"\x00", 1)[0].decode("latin1")
    return text.strip().upper()


def _clean_bif_name(raw: bytes) -> str:
    text = raw.split(b"\x00", 1)[0].decode("latin1").strip()
    return text.replace("\\", "/").replace(":", "/").lstrip("/")


def find_bif(game: Path, name: str) -> Optional[Path]:
    """Locate a BIF/CBF next to CHITIN.KEY, under data/, or on a CD folder."""
    rel = Path(name)
    rel_names = [rel, Path(rel.name)]
    roots = [game, game / "data"]
    for i in range(1, 7):
        roots += [game / f"cd{i}", game / f"CD{i}"]
    for root in roots:
        for candidate in rel_names:
            path = root / candidate
            if path.is_file():
                return path
            if path.suffix.lower() == ".bif":
                cbf = path.with_suffix(".cbf")
                if cbf.is_file():
                    return cbf
    return None


def _decompress_chunks(data: bytes, start: int, total: int) -> bytes:
    out = bytearray()
    off = start
    while len(out) < total:
        if off + 8 > len(data):
            break
        declen, complen = struct.unpack_from("<II", data, off)
        off += 8
        out += zlib.decompress(data[off:off + complen])
        off += complen
        if complen == 0:
            break
    return bytes(out)


def materialize_bif(path: Path, cache: Path) -> Path:
    """Return a plain BIFF V1 path, inflating compressed archives into cache."""
    with path.open("rb") as handle:
        sig = handle.read(8)
        if sig == b"BIFFV1  ":
            return path
        rest = handle.read()
    blob = sig + rest
    if sig == b"BIFCV1.0":
        total = struct.unpack_from("<I", blob, 8)[0]
        payload = _decompress_chunks(blob, 12, total)
    elif sig == b"BIF V1.0":
        fnlen = struct.unpack_from("<I", blob, 8)[0]
        base = 12 + fnlen
        complen = struct.unpack_from("<I", blob, base + 4)[0]
        payload = zlib.decompress(blob[base + 8:base + 8 + complen])
    else:
        raise ValueError(f"unknown bif signature {sig!r} in {path}")
    if payload[:8] != b"BIFFV1  ":
        raise ValueError(f"{path.name} did not inflate to a BIFF")
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / (path.name + ".bin")
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(out)
    return out


class _Bif:
    def __init__(self, path: Path, cache: Path):
        self.path = path
        self.cache = cache
        self._data_path: Optional[Path] = None
        self._files: Dict[int, Tuple[int, int]] = {}

    def _ensure(self) -> Path:
        if self._data_path is not None:
            return self._data_path
        data_path = materialize_bif(self.path, self.cache)
        with data_path.open("rb") as handle:
            header = handle.read(20)
            file_count, _tile_count, file_off = struct.unpack_from("<3I", header, 8)
            handle.seek(file_off)
            file_blob = handle.read(file_count * 16)
        files: Dict[int, Tuple[int, int]] = {}
        for i in range(file_count):
            loc, off, size, _typ, _unk = struct.unpack_from("<3I2H", file_blob, i * 16)
            files.setdefault(loc & 0x3FFF, (off, size))
        self._files = files
        self._data_path = data_path
        return data_path

    def read(self, locator: int) -> bytes:
        data_path = self._ensure()
        entry = self._files.get(locator & 0x3FFF)
        if entry is None:
            raise KeyError(f"locator {locator:#x} missing in {self.path.name}")
        off, size = entry
        with data_path.open("rb") as handle:
            handle.seek(off)
            buf = handle.read(size)
        if len(buf) != size:
            raise ValueError(f"short read in {self.path.name} at {off}")
        return buf


class KeyIndex:
    """CHITIN.KEY + override resources, keyed by lowercase extension (override wins)."""

    def __init__(self, game: Path, cache: Path = BIF_CACHE, types: Optional[Set[int]] = None):
        self.game = game
        self.cache = cache
        self.types = types or set(EXT_BY_TYPE)
        self.loaders: Dict[str, Dict[str, Callable[[], bytes]]] = {
            EXT_BY_TYPE[typ]: {} for typ in self.types if typ in EXT_BY_TYPE
        }
        self._build()

    def _add(self, ext: str, name: str, loader: Callable[[], bytes], replace: bool) -> None:
        name = name.strip().upper()
        bucket = self.loaders.get(ext)
        if name and bucket is not None and (replace or name not in bucket):
            bucket[name] = loader

    def _build(self) -> None:
        key_path = self.game / "CHITIN.KEY"
        if not key_path.is_file():
            key_path = self.game / "chitin.key"
        data = key_path.read_bytes()
        if data[:8] != b"KEY V1  ":
            raise ValueError(f"{key_path} is not a KEY V1 file")
        bif_count, res_count, bif_off, res_off = struct.unpack_from("<4I", data, 8)
        bifs: List[Optional[_Bif]] = []
        for i in range(bif_count):
            entry = bif_off + i * 12
            name_off = struct.unpack_from("<I", data, entry + 4)[0]
            name_len = struct.unpack_from("<H", data, entry + 8)[0]
            rel = _clean_bif_name(data[name_off:name_off + name_len])
            found = find_bif(self.game, rel) if rel else None
            bifs.append(_Bif(found, self.cache) if found else None)
        for i in range(res_count):
            entry = res_off + i * 14
            name = _resref(data[entry:entry + 8])
            typ = struct.unpack_from("<H", data, entry + 8)[0]
            locator = struct.unpack_from("<I", data, entry + 10)[0]
            ext = EXT_BY_TYPE.get(typ, "")
            if typ not in self.types or not name or not ext:
                continue
            bif_index = (locator >> 20) & 0xFFF
            if bif_index >= len(bifs) or bifs[bif_index] is None:
                continue
            bif = bifs[bif_index]
            self._add(ext, name, lambda loc=locator, b=bif: b.read(loc), replace=False)
        override = self.game / "override"
        if override.is_dir():
            wanted_ext = {EXT_BY_TYPE[typ] for typ in self.types if typ in EXT_BY_TYPE}
            for path in override.rglob("*"):
                ext = path.suffix.lower().lstrip(".")
                if path.is_file() and ext in wanted_ext:
                    self._add(ext, path.stem, lambda p=path: p.read_bytes(), replace=True)

    def names(self, ext: str) -> List[str]:
        return sorted(self.loaders.get(ext, {}))

    def get(self, name: str, ext: str) -> bytes:
        return self.loaders[ext][name.upper()]()


class AreIndex:
    """ARE resources from CHITIN.KEY + override (override wins)."""

    def __init__(self, game: Path, cache: Path = BIF_CACHE):
        self._index = KeyIndex(game, cache, types={ARE_TYPE})

    def names(self) -> List[str]:
        return self._index.names("are")

    def get(self, name: str) -> bytes:
        return self._index.get(name, "are")


class Tlk:
    """Classic dialog.tlk (TLK V1). Newlines become spaces so checksums match."""

    def __init__(self, path: Path):
        self.data = path.read_bytes()
        if self.data[:8] != b"TLK V1  ":
            raise SystemExit(f"{path} is not a TLK V1 file")
        self.count = struct.unpack_from("<I", self.data, 0x0A)[0]
        self.base = struct.unpack_from("<I", self.data, 0x0E)[0]

    def text(self, strref: int) -> str:
        if strref <= 0 or strref >= self.count or strref == INVALID:
            return ""
        entry = 0x12 + strref * 26
        off = struct.unpack_from("<I", self.data, entry + 18)[0]
        length = struct.unpack_from("<I", self.data, entry + 22)[0]
        raw = self.data[self.base + off:self.base + off + length]
        return raw.decode("cp1252", "replace").replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()


# ==================== ARE regions ====================

def parse_regions(data: bytes) -> List[Dict]:
    """IESDP ARE v1.0 / v9.1 region table (0xC4 bytes each)."""
    sig = data[:8]
    if sig not in (b"AREAV1.0", b"AREAV9.1"):
        return []
    extra = 16 if sig == b"AREAV9.1" else 0
    base = 0x54 + extra
    count = struct.unpack_from("<H", data, base + 6)[0]
    off = struct.unpack_from("<I", data, base + 8)[0]
    out = []
    for i in range(count):
        o = off + i * REGION_SIZE
        if o + REGION_SIZE > len(data):
            break
        rec = data[o:o + REGION_SIZE]
        dlg = rec[0xBC:0xC4].split(b"\x00", 1)[0].decode("latin1", "replace")
        if not dlg or not dlg.isascii() or not dlg.isprintable():
            dlg = ""
        out.append({
            "type": struct.unpack_from("<H", rec, 0x20)[0],
            "strref": struct.unpack_from("<I", rec, 0x64)[0],
            "dlg": dlg.strip().upper(),
        })
    return out


def rows_from_regions(regions: Sequence[Dict], texts: Dict[int, str],
                      skip: Optional[Set[int]] = None) -> List[Dict]:
    """Unique examine lines: info type, real strref, no dialog, non-empty text."""
    skip = skip or set()
    seen: Set[int] = set()
    rows: List[Dict] = []
    for region in regions:
        if region["type"] != INFO_TYPE:
            continue
        strref = region["strref"]
        if strref <= 0 or strref == INVALID or strref in seen or strref in skip:
            continue
        if region["dlg"]:
            continue
        text = texts.get(strref, "")
        if not text:
            continue
        seen.add(strref)
        rows.append({
            "StrRef": strref,
            "Speaker": NARRATOR,
            "Action": "generate",
            "SoundResRef": fallback_resref(strref),
            "Text": text,
            "Voice": "",
        })
    rows.sort(key=lambda row: row["StrRef"])
    return rows


def _dialog_tlk(game: Path) -> Path:
    for name in ("dialog.tlk", "Dialog.tlk"):
        path = game / name
        if path.is_file():
            return path
    raise SystemExit(f"No dialog.tlk in {game}")


def collect(game: Path) -> List[Dict]:
    """Every qualifying info-point strref in CHITIN.KEY + override."""
    index = AreIndex(game)
    tlk = Tlk(_dialog_tlk(game))
    regions: List[Dict] = []
    for name in index.names():
        regions.extend(parse_regions(index.get(name)))
    texts: Dict[int, str] = {}
    for region in regions:
        strref = region["strref"]
        if strref not in texts:
            texts[strref] = tlk.text(strref)
    return rows_from_regions(regions, texts)


# ==================== checksum / IO ====================

def load_plan_strrefs(path: Path) -> Set[int]:
    """Strrefs already listed in a plan.csv (or any CSV with a StrRef column)."""
    if not path.exists():
        return set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {int(row["StrRef"]) for row in csv.DictReader(handle) if row.get("StrRef")}


def checksum(rows: Sequence[Dict]) -> Tuple[int, int, int, int, int]:
    """(count, chars, strref sum, min, max)."""
    refs = [int(r["StrRef"]) for r in rows]
    chars = sum(len(r["Text"]) for r in rows)
    return len(rows), chars, sum(refs), (min(refs) if refs else 0), (max(refs) if refs else 0)


def verify(rows: Sequence[Dict]) -> None:
    """Stop if the extract does not match the known classic-PST totals."""
    got = checksum(rows)
    expected = (EXPECTED_COUNT, EXPECTED_CHARS, EXPECTED_SUM, EXPECTED_MIN, EXPECTED_MAX)
    if got != expected:
        raise SystemExit(
            f"examine-line count differs from expected.\n"
            f"  expected: {EXPECTED_COUNT} lines, {EXPECTED_CHARS} chars, "
            f"strref sum {EXPECTED_SUM}, min {EXPECTED_MIN}, max {EXPECTED_MAX}\n"
            f"  got:      {got[0]} lines, {got[1]} chars, "
            f"strref sum {got[2]}, min {got[3]}, max {got[4]}"
        )


def write_rows(rows: Sequence[Dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_rows(path: Path) -> List[Dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = []
        for raw in csv.DictReader(handle):
            raw["StrRef"] = int(raw["StrRef"])
            raw["Voice"] = raw.get("Voice") or ""
            rows.append(raw)
        return rows


def dicts_to_plan(rows: Sequence[Dict]) -> List[PlanRow]:
    return [PlanRow(int(r["StrRef"]), r["Speaker"], r["Action"], r["SoundResRef"], r["Text"],
                    r.get("Voice") or "", r.get("OriginalResRef") or "") for r in rows]


def merge_into_plan(plan: List[PlanRow], extra: Sequence[PlanRow]) -> Tuple[List[PlanRow], int]:
    """Append extra rows whose strrefs are not already in the plan; sort by strref."""
    have = {p.strref for p in plan}
    added = [p for p in extra if p.strref not in have]
    out = list(plan) + added
    out.sort(key=lambda p: p.strref)
    return out, len(added)


def keep_existing(plan: List[PlanRow], previous: Sequence[PlanRow]) -> List[PlanRow]:
    """Keep previously imported examine rows when the dialog plan is rebuilt."""
    extra = [p for p in previous if is_infopoint(p)]
    merged, _n = merge_into_plan(plan, extra)
    return merged


def load_or_extract(game: Path) -> List[Dict]:
    """Reuse infopoints.csv when it still matches the known totals; else extract."""
    if INFOPOINTS_PATH.exists():
        rows = read_rows(INFOPOINTS_PATH)
        try:
            verify(rows)
            return rows
        except SystemExit:
            logger.warning("infopoints.csv checksum mismatch; re-extracting from the game")
    rows = collect(game)
    verify(rows)
    write_rows(rows, INFOPOINTS_PATH)
    logger.info(f"📋 {INFOPOINTS_PATH}: {len(rows)} examine lines")
    return rows




def gemrb_sync_cmd(vve: Path) -> Optional[List[str]]:
    """Argv for GemRB sync. Only Drama 3 audio_fish counts as voiced (OmniVoice is archived)."""
    if not GEMRB_SYNC.is_file():
        return None
    return [sys.executable, str(GEMRB_SYNC), "--vve", str(vve),
            "--audio-dir", FISH_AUDIO_DIR.name]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game", type=Path, default=Path(cfg.GAME_DIRECTORY),
                        help="Classic PST install (CHITIN.KEY + dialog.tlk)")
    parser.add_argument("--csv", type=Path, default=INFOPOINTS_PATH, help="Write the full extract here")
    parser.add_argument("--merge", action="store_true", help="Append examine rows to plan.csv (no duplicates)")
    parser.add_argument("--limit", type=int, default=0,
                        help=f"Only merge the first N examine lines (0 = all; GUI test uses {TEST_LIMIT})")
    args = parser.parse_args()

    rows = load_or_extract(args.game)
    if args.csv != INFOPOINTS_PATH or not INFOPOINTS_PATH.exists():
        write_rows(rows, args.csv)
    count, chars, total, lo, hi = checksum(rows)
    logger.info(f"# {count} unique examine lines, {chars} chars, strref sum {total}, min {lo}, max {hi}")

    if not args.merge:
        return
    extra = dicts_to_plan(rows[:args.limit] if args.limit else rows)
    plan = read_plan(PLAN_PATH) if PLAN_PATH.exists() else []
    merged, added = merge_into_plan(plan, extra)
    write_plan(merged, PLAN_PATH)
    write_plan_meta(merged)
    logger.info(f"📋 {PLAN_PATH}: added {added} examine lines "
                f"({sum(1 for p in merged if is_infopoint(p))} Narrator rows in plan)")


if __name__ == "__main__":
    main()
