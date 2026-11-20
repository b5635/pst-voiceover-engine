#!/usr/bin/env python3
"""
PST idle barks (BCS FloatMessage / FloatMessageRnd overhead lines).

These are not dialog. Collectors yelling "Deaders?", workers, ghouls, SRC
pools. Many Drowned Nations ghouls (and other INI critters) get their AI from
area spawn INI `script_default` / `script_override`, not the CRE/ARE slots.
GemRB already plays the TLK sound on that strref, or VV<strref>.wav by name
if the slot is still empty. Starting a real conversation kills that speech.
Do not STRING_SET / rewrite the text.

    python pst_floats.py                  # extract, verify, write floats.csv
    python pst_floats.py --merge

Same plan columns as dialog/examine. Action=generate, SoundResRef=VV<strref>,
Voice from casting.json. Shared barks get one row / one WAV.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from libs.appconfig import cfg
from libs.utils import setup_logging
from pst_infopoints import (
    ARE_TYPE,
    BCS_TYPE,
    BIF_CACHE,
    CRE_TYPE,
    FISH_AUDIO_DIR,
    INI_TYPE,
    INVALID,
    KeyIndex,
    PLAN_PATH,
    SRC_TYPE,
    Tlk,
    _dialog_tlk,
    checksum,
    dicts_to_plan,
    fallback_resref,
    load_plan_strrefs,
    merge_into_plan,
    read_rows,
    write_rows,
)
from pst_revoice import (
    NARRATOR,
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
FLOATS_PATH = Path("pst_revoice/floats.csv")
CRE_SCRIPTS = (0x248, 0x250, 0x258, 0x260, 0x268)
CRE_DIALOG = 0x370
CRE_LONGNAME = 0x08
FLOAT_TYPES = {BCS_TYPE, CRE_TYPE, SRC_TYPE, ARE_TYPE, INI_TYPE}
# GemRB IniSpawn.cpp AssignScripts — script_name is a death variable, not a BCS.
INI_SCRIPT_KEYS = (
    "script_override", "script_class", "script_race", "script_general",
    "script_default", "script_area", "script_specifics",
    "script_special_1", "script_special_2", "script_special_3",
    "script_team", "script_combat", "script_movement",
)
# Drowned Nations INI ghouls (1700GUL1/2/6); old dumps labeled these Unknown.
INI_IDENTITY_STRREFS = (25828, 25829, 25830, 25831)

FLOAT_RE = re.compile(
    r'AC\n210OB\n.*?""OB\nOB\n.*?""OB\nOB\n.*?""OB\n(\d+) 0 0 0 0',
    re.S,
)
RND_RE = re.compile(
    r'AC\n212OB\n.*?""OB\nOB\n.*?""OB\nOB\n.*?""OB\n0 0 0 0 0"([^"]*)"',
    re.S,
)

# Against classic PST + current plan.csv (dialog lines already skipped):
EXPECTED_COUNT = 782
EXPECTED_CHARS = 28026
EXPECTED_SUM = 27121504
EXPECTED_MIN = 1317
EXPECTED_MAX = 68077
TEST_STRREFS = (8161, 8163, 8164, 8165, 51046, 25828, 25829, 25830, 25831, 25832, 25833)
# =======================================================


def looks_spoken(text: str) -> bool:
    """True for overhead speech/emotes; false for item text and VB slot labels."""
    t = text.strip()
    if len(t) < 2:
        return False
    if t[0] in "\"'*":
        return True
    return t.startswith("::")


def parse_src(data: bytes) -> List[int]:
    """Strrefs from an SRC pool (uint32 count, then strref+weight pairs)."""
    if len(data) < 4:
        return []
    count = struct.unpack_from("<I", data, 0)[0]
    refs = []
    for i in range(count):
        off = 4 + i * 8
        if off + 4 > len(data):
            break
        refs.append(struct.unpack_from("<I", data, off)[0])
    return refs


def parse_bcs_floats(data: bytes) -> Tuple[List[int], List[str]]:
    """(FloatMessage strrefs, FloatMessageRnd pool names) from compiled BCS."""
    text = data.decode("latin1", "replace")
    refs = [int(m) for m in FLOAT_RE.findall(text)]
    pools = [m.upper() for m in RND_RE.findall(text) if m.strip()]
    return refs, pools


def field_resref(raw: bytes) -> str:
    """8-byte resref; empty when missing or NONE."""
    text = raw.split(b"\x00", 1)[0].decode("latin1", "replace")
    if not text or not text.isascii() or not text.isprintable():
        return ""
    name = text.strip().upper()
    return "" if name in ("", "NONE") else name


def cre_resref(data: bytes, off: int) -> str:
    if off + 8 > len(data) or data[:8] != b"CRE V1.2":
        return ""
    return field_resref(data[off:off + 8])


def cre_long_name_strref(data: bytes) -> int:
    if len(data) < 12 or data[:8] != b"CRE V1.2":
        return 0
    return struct.unpack_from("<I", data, CRE_LONGNAME)[0]


def cre_short_name_strref(data: bytes) -> int:
    if len(data) < 16 or data[:8] != b"CRE V1.2":
        return 0
    return struct.unpack_from("<I", data, 0x0C)[0]


def cre_display_name(data: bytes, tlk: Tlk) -> str:
    """Long name, then short name. Empty when the CRE has no TLK name."""
    return tlk.text(cre_long_name_strref(data)) or tlk.text(cre_short_name_strref(data))


def token_resref(value: str) -> str:
    """INI/ARE token to an 8-char resref; empty when missing or NONE."""
    parts = (value or "").strip().split()
    if not parts:
        return ""
    token = parts[0].split(";", 1)[0]
    if "." in token:
        token = token.split(".", 1)[0]
    if not token:
        return ""
    raw = token.encode("latin1", "replace")[:8]
    return field_resref(raw.ljust(8, b"\x00"))


def parse_ini_spawns(data: bytes) -> List[Tuple[str, str, List[str]]]:
    """(cre, dialog, scripts) from each area-INI critter that has cre_file.

    PST Drowned Nations ghouls (and Hive workers, rats, …) are spawned from
    AR*.INI, not placed as ARE actors. script_name is a death variable.
    """
    sections: Dict[str, Dict[str, str]] = {}
    current = ""
    for raw in data.decode("latin1", "replace").splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            sections[current] = {}
            continue
        if not current or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip().lower(), val.strip()
        if key and val:
            sections[current][key] = val
    out: List[Tuple[str, str, List[str]]] = []
    for fields in sections.values():
        cre_field = fields.get("cre_file") or ""
        if not cre_field:
            continue
        dlg = token_resref(fields.get("dialog") or "")
        scripts = [token_resref(fields.get(key) or "") for key in INI_SCRIPT_KEYS]
        for cre_token in cre_field.split(","):
            cre = token_resref(cre_token)
            if cre:
                out.append((cre, dlg, scripts))
    return out


def bind_actor(script_dialogs: Dict[str, List[str]], script_names: Dict[str, List[str]],
               cre_dialog: Dict[str, str], cre_name: Dict[str, str],
               index: KeyIndex, tlk: Tlk, cre: str, dlg: str,
               scripts: Sequence[str]) -> None:
    """Copy CRE/INI/ARE dialog and long name onto every script that creature runs."""
    if not dlg:
        dlg = cre_dialog.get(cre, "")
    long_name = cre_name.get(cre, "")
    if not long_name and cre:
        try:
            blob = index.get(cre, "cre")
            long_name = cre_display_name(blob, tlk)
            cre_name[cre] = long_name
        except KeyError:
            long_name = ""
    for script in scripts:
        if not script:
            continue
        if dlg:
            script_dialogs[script].append(dlg)
        if long_name:
            script_names[script].append(long_name)


def parse_are_actors(data: bytes) -> List[Tuple[str, str, List[str]]]:
    """(cre, dialog, scripts) from each ARE actor stub (0x110)."""
    sig = data[:8]
    if sig not in (b"AREAV1.0", b"AREAV9.1"):
        return []
    extra = 16 if sig == b"AREAV9.1" else 0
    base = 0x54 + extra
    if base + 6 > len(data):
        return []
    off = struct.unpack_from("<I", data, base)[0]
    count = struct.unpack_from("<H", data, base + 4)[0]
    out = []
    for i in range(count):
        rec = data[off + i * 0x110:off + (i + 1) * 0x110]
        if len(rec) < 0x88:
            break
        dlg = field_resref(rec[0x48:0x50])
        scripts = [field_resref(rec[0x50 + j * 8:0x58 + j * 8]) for j in range(6)]
        cre = field_resref(rec[0x80:0x88])
        out.append((cre, dlg, scripts))
    return out


def load_vve_voices(vve: Path) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, List[Tuple[str, str]]]]:
    """
    dialog resref -> speaker, dialog resref -> voice_id, exact TLK text ->
    [(speaker, voice_id), ...] from npcs.json / casting.json.
    """
    speakers: Dict[str, str] = {}
    voices: Dict[str, str] = {}
    speaker_voice: Dict[str, str] = {}
    npcs_path = vve / "pst_cast" / "npcs.json"
    casting_path = vve / "pst_cast" / "casting.json"
    if casting_path.is_file():
        raw = json.loads(casting_path.read_text(encoding="utf-8"))
        for key, row in raw.items():
            dlg = str(row.get("dialog") or key).upper()
            speaker = row.get("speaker") or ""
            voice_id = row.get("voice_id") or ""
            if dlg and speaker:
                speakers[dlg] = speaker
            if dlg and voice_id:
                voices[dlg] = voice_id
            if speaker and voice_id:
                speaker_voice[speaker] = voice_id
    by_text: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    if npcs_path.is_file():
        for npc in json.loads(npcs_path.read_text(encoding="utf-8")):
            dlg = str(npc.get("dialog", "")).upper()
            speaker = npc.get("speaker") or speakers.get(dlg, "")
            if dlg and speaker:
                speakers.setdefault(dlg, speaker)
            voice_id = voices.get(dlg, "") or speaker_voice.get(speaker, "")
            for line in npc.get("lines") or []:
                text = (line.get("text") or "").strip()
                if text and speaker:
                    by_text[text].append((speaker, voice_id))
    return speakers, voices, dict(by_text)


def pick_identity(dialogs: Sequence[str], cre_names: Sequence[str],
                  speakers: Dict[str, str], voices: Dict[str, str],
                  text: str, dlg_by_text: Dict[str, List[Tuple[str, str]]]) -> Tuple[str, str]:
    """
    Speaker and library voice for one bark.

    Dialog resref wins when the creature has one; otherwise CRE long name.
    If this exact text already exists as a DLG line for that speaker, reuse
    that voice. Never Narrator.
    """
    usable = [d for d in dialogs if d and d != "NONE"]
    names = [n for n in cre_names if n]
    speaker = ""
    voice = ""
    if usable:
        dlg = Counter(usable).most_common(1)[0][0]
        speaker = speakers.get(dlg, "")
        voice = voices.get(dlg, "")
        if not speaker and names:
            speaker = Counter(names).most_common(1)[0][0]
        elif not speaker:
            speaker = dlg
    elif names:
        speaker = Counter(names).most_common(1)[0][0]
    hits = dlg_by_text.get(text) or []
    if speaker:
        for sp, vid in hits:
            if sp == speaker and vid:
                voice = vid
                break
    elif hits:
        speaker, voice = hits[0]
    if speaker == NARRATOR:
        speaker = Counter(names).most_common(1)[0][0] if names else ""
        if speaker == NARRATOR:
            speaker = ""
    if not speaker:
        speaker = "Unknown"
    return speaker, voice


def rows_from_hits(hits: Dict[int, List[str]], script_dialogs: Dict[str, List[str]],
                   script_names: Dict[str, List[str]], texts: Dict[int, str],
                   speakers: Dict[str, str], voices: Dict[str, str],
                   dlg_by_text: Optional[Dict[str, List[Tuple[str, str]]]] = None,
                   skip: Optional[Set[int]] = None) -> List[Dict]:
    """One generate row per spoken strref, skipping plan/empty/VB labels."""
    skip = skip or set()
    dlg_by_text = dlg_by_text or {}
    rows = []
    for strref in sorted(hits):
        if strref <= 0 or strref == INVALID or strref in skip:
            continue
        text = texts.get(strref, "")
        if not looks_spoken(text):
            continue
        dialogs: List[str] = []
        names: List[str] = []
        for script in hits[strref]:
            dialogs.extend(script_dialogs.get(script, []))
            names.extend(script_names.get(script, []))
        speaker, voice = pick_identity(dialogs, names, speakers, voices, text, dlg_by_text)
        rows.append({
            "StrRef": strref,
            "Speaker": speaker,
            "Action": "generate",
            "SoundResRef": fallback_resref(strref),
            "Text": text,
            "Voice": voice,
        })
    return rows


def collect(game: Path, skip: Optional[Set[int]] = None, vve: Optional[Path] = None) -> List[Dict]:
    """Every qualifying FloatMessage / SRC-pool strref, minus skip."""
    index = KeyIndex(game, BIF_CACHE, types=FLOAT_TYPES)
    tlk = Tlk(_dialog_tlk(game))
    vve = vve or Path(".")
    speakers, voices, dlg_by_text = load_vve_voices(vve)

    src_refs: Dict[str, List[int]] = {}
    for name in index.names("src"):
        src_refs[name] = parse_src(index.get(name, "src"))

    hits: Dict[int, List[str]] = defaultdict(list)
    for name in index.names("bcs"):
        refs, pools = parse_bcs_floats(index.get(name, "bcs"))
        for ref in refs:
            hits[ref].append(name)
        for pool in pools:
            for ref in src_refs.get(pool, []):
                hits[ref].append(name)

    cre_dialog: Dict[str, str] = {}
    cre_name: Dict[str, str] = {}
    script_dialogs: Dict[str, List[str]] = defaultdict(list)
    script_names: Dict[str, List[str]] = defaultdict(list)
    for name in index.names("cre"):
        cre = index.get(name, "cre")
        dlg = cre_resref(cre, CRE_DIALOG)
        long_name = cre_display_name(cre, tlk)
        cre_dialog[name] = dlg
        cre_name[name] = long_name
        # Same-named BCS is the usual default AI script when the slots are empty.
        bound = [name]
        for off in CRE_SCRIPTS:
            script = cre_resref(cre, off)
            if script:
                bound.append(script)
        for script in bound:
            if dlg:
                script_dialogs[script].append(dlg)
            if long_name:
                script_names[script].append(long_name)
    for name in index.names("are"):
        for cre, dlg, scripts in parse_are_actors(index.get(name, "are")):
            bind_actor(script_dialogs, script_names, cre_dialog, cre_name,
                       index, tlk, cre, dlg, scripts)
    for name in index.names("ini"):
        for cre, dlg, scripts in parse_ini_spawns(index.get(name, "ini")):
            bind_actor(script_dialogs, script_names, cre_dialog, cre_name,
                       index, tlk, cre, dlg, scripts)

    needed = {ref for ref in hits if ref > 0 and ref != INVALID}
    texts = {ref: tlk.text(ref) for ref in needed}
    return rows_from_hits(hits, script_dialogs, script_names, texts, speakers, voices,
                          dlg_by_text, skip)


def verify(rows: Sequence[Dict]) -> None:
    """Stop if the extract does not match the known classic-PST totals."""
    got = checksum(rows)
    expected = (EXPECTED_COUNT, EXPECTED_CHARS, EXPECTED_SUM, EXPECTED_MIN, EXPECTED_MAX)
    if got != expected:
        raise SystemExit(
            f"idle-bark count differs from expected.\n"
            f"  expected: {EXPECTED_COUNT} lines, {EXPECTED_CHARS} chars, "
            f"strref sum {EXPECTED_SUM}, min {EXPECTED_MIN}, max {EXPECTED_MAX}\n"
            f"  got:      {got[0]} lines, {got[1]} chars, "
            f"strref sum {got[2]}, min {got[3]}, max {got[4]}"
        )


def dialog_plan_strrefs(plan_path: Path = PLAN_PATH, floats_path: Path = FLOATS_PATH) -> Set[int]:
    """plan.csv strrefs that are not idle barks (so re-extract still matches 782)."""
    return load_plan_strrefs(plan_path) - load_plan_strrefs(floats_path)


@lru_cache(maxsize=4)
def float_strrefs(path: str = "") -> frozenset:
    """Strrefs in floats.csv; empty if the extract has not been written yet."""
    target = Path(path) if path else FLOATS_PATH
    if not target.exists():
        return frozenset()
    return frozenset(load_plan_strrefs(target))


def is_float(row: PlanRow, path: Optional[Path] = None) -> bool:
    """True for BCS idle-bark rows (registry is floats.csv)."""
    return row.strref in float_strrefs(str(path or FLOATS_PATH))


def keep_existing(plan: List[PlanRow], previous: Sequence[PlanRow]) -> List[PlanRow]:
    """Keep previously imported idle-bark rows when the dialog plan is rebuilt."""
    extra = [p for p in previous if is_float(p)]
    merged, _n = merge_into_plan(plan, extra)
    return merged


def _csv_stale(rows: Sequence[Dict]) -> bool:
    """True when a dump used Narrator, left Speaker blank, or missed INI spawns."""
    if any((r.get("Speaker") or "") in ("", NARRATOR) for r in rows):
        return True
    by_ref = {int(r["StrRef"]): r for r in rows if r.get("StrRef")}
    return any(
        (by_ref.get(ref) or {}).get("Speaker") == "Unknown"
        for ref in INI_IDENTITY_STRREFS
        if ref in by_ref
    )


def load_or_extract(game: Path, vve: Path = Path(".")) -> List[Dict]:
    """Reuse floats.csv when it still matches the known totals; else extract."""
    skip = dialog_plan_strrefs()
    if FLOATS_PATH.exists():
        rows = read_rows(FLOATS_PATH)
        try:
            verify(rows)
            if not _csv_stale(rows):
                return rows
            logger.warning("floats.csv has blank, Narrator, or INI-spawn Unknown speakers; re-extracting")
        except SystemExit:
            logger.warning("floats.csv checksum mismatch; re-extracting from the game")
    rows = collect(game, skip, vve)
    verify(rows)
    write_rows(rows, FLOATS_PATH)
    float_strrefs.cache_clear()
    logger.info(f"📋 {FLOATS_PATH}: {len(rows)} idle barks")
    return rows




def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game", type=Path, default=Path(cfg.GAME_DIRECTORY),
                        help="Classic PST install (CHITIN.KEY + dialog.tlk)")
    parser.add_argument("--csv", type=Path, default=FLOATS_PATH, help="Write the full extract here")
    parser.add_argument("--vve", type=Path, default=Path("."),
                        help="Repo root for pst_cast/npcs.json and casting.json")
    parser.add_argument("--merge", action="store_true", help="Append idle-bark rows to plan.csv (no duplicates)")
    parser.add_argument("--limit", type=int, default=0, help="Only merge the first N idle barks (0 = all)")
    parser.add_argument("--strref", type=int, action="append", help="Only merge these strrefs (repeatable)")
    args = parser.parse_args()

    rows = load_or_extract(args.game, args.vve)
    if args.csv != FLOATS_PATH or not FLOATS_PATH.exists():
        write_rows(rows, args.csv)
        float_strrefs.cache_clear()
    count, chars, total, lo, hi = checksum(rows)
    logger.info(f"# {count} unique idle barks, {chars} chars, strref sum {total}, min {lo}, max {hi}")

    if not args.merge:
        return
    extra_rows = rows
    if args.strref:
        wanted = set(args.strref)
        extra_rows = [r for r in extra_rows if int(r["StrRef"]) in wanted]
    if args.limit:
        extra_rows = extra_rows[:args.limit]
    extra = dicts_to_plan(extra_rows)
    plan = read_plan(PLAN_PATH) if PLAN_PATH.exists() else []
    merged, added = merge_into_plan(plan, extra)
    write_plan(merged, PLAN_PATH)
    write_plan_meta(merged)
    logger.info(f"📋 {PLAN_PATH}: added {added} idle barks "
                f"({sum(1 for p in merged if is_float(p))} float rows in plan)")


if __name__ == "__main__":
    main()
