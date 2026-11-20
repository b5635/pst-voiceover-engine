#!/usr/bin/env python3
"""
PST NPC Profiles
Gathers, for every Planescape: Torment dialog (DLG), who speaks it and what
they look like, as input for LLM voice casting (pst_cast.py).

Per dialog it records:
    - every creature (CRE) that uses it: name, gender, race, class and its
      animation (appearance), e.g. "Dustman, Female" or "Zombie, Male"
    - every NPC line (SAY text, in state order); player replies are left out
    - whether the speaker already has a voice (voices_pst/, pst-voice-aliases.json)

Reads the extracted DLG/CRE files and IDS tables in cfg.EXTRACT_DIR (run
dialog-report-prepare.py first; the IDS files come from WeiDU --biff-get-rest)
and the dialog report CSV.

    python pst_npc_profiles.py          -> pst_cast/npcs.json
"""

import argparse
import csv
import json
import re
import struct
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from libs.appconfig import cfg
from libs.utils import find_dialog_tlk, iter_files_ci, parse_dialog_tlk

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# ==================== CONFIGURATION ====================
CAST_DIR = Path("pst_cast")
NPCS_PATH = CAST_DIR / "npcs.json"
VOICED_DIR = Path("voices_pst")                    # "<Name> N.WAV" = existing cloned voice
ALIASES_PATH = Path("pst-voice-aliases.json")

# CRE V1.2 (PST) offsets
CRE_NAME = 0x0008
CRE_ANIMATION = 0x0028
CRE_RACE = 0x0316
CRE_CLASS = 0x0317
CRE_GENDER = 0x0319
CRE_FACTION = 0x0313       # FACTION.IDS (Dustmen, Harmonium, ...)
CRE_ALIGNMENT = 0x031F     # ALIGNMEN.IDS
CRE_DIALOG = 0x0370
# ARE V1.0 actor table
ARE_ACTORS_OFFSET = 0x54
ARE_ACTORS_COUNT = 0x58
ARE_ACTOR_SIZE = 0x110
PLAYER_NAME = "Nameless One"
GENDERS = {1: "male", 2: "female", 3: "other", 4: "neither", 5: "both"}
# =======================================================


def read_ids(path: Path) -> Dict[int, str]:
    """IDS file -> {value: name}."""
    result: Dict[int, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="latin-1").splitlines()[1:]:
        parts = line.split(None, 1)
        if len(parts) == 2:
            try:
                result[int(parts[0], 0)] = parts[1].strip()
            except ValueError:
                pass
    return result


def ensure_extracted(extract_dir: Path, sub: str, pattern: str, probe: str) -> Path:
    """
    Extract game resources matching `pattern` into extract_dir/sub with WeiDU,
    unless `probe` is already there. --biff-get-rest writes to its working
    directory, so WeiDU runs inside the target folder.
    """
    out = extract_dir / sub
    if (out / probe).exists():
        return out
    out.mkdir(parents=True, exist_ok=True)
    weidu = Path(cfg.WEIDU_PATH).resolve()
    subprocess.run([str(weidu), "--game", str(Path(cfg.GAME_DIRECTORY).resolve()), "--biff-get-rest", pattern],
                   cwd=out, capture_output=True)
    return out


def area_actors(are_dir: Path) -> List[Dict]:
    """
    Actors placed in area files that set their own dialog (overriding the CRE's).
    Many NPCs (e.g. the Hive merchants) only get their dialog this way.
    """
    actors = []
    for path in iter_files_ci(are_dir, "are"):
        data = path.read_bytes()
        if data[:8] != b"AREAV1.0":
            continue
        off, = struct.unpack_from("<I", data, ARE_ACTORS_OFFSET)
        count, = struct.unpack_from("<H", data, ARE_ACTORS_COUNT)
        for i in range(count):
            a = data[off + i * ARE_ACTOR_SIZE:off + (i + 1) * ARE_ACTOR_SIZE]
            if len(a) < ARE_ACTOR_SIZE:
                break
            actors.append({
                "area": path.stem.upper(),
                "actor": a[:32].split(b"\x00", 1)[0].decode("latin-1").strip(),
                "animation": struct.unpack_from("<I", a, 0x30)[0],
                "dialog": a[0x48:0x50].split(b"\x00", 1)[0].decode("ascii", "replace").upper(),
                "cre": a[0x80:0x88].split(b"\x00", 1)[0].decode("ascii", "replace").upper(),
            })
    return actors


def clean_animation(name: str) -> str:
    """'(cl 2) Dustman, Male' -> 'Dustman, Male'; drops dev notes like '(DON'T USE!!!)'."""
    name = re.sub(r"\((?:cl \d+|DON'T USE!+)\)\s*", "", name, flags=re.I)
    return re.sub(r"\s+KillMe\b", "", name).strip()


def parse_cre(path: Path, tlk, anim, race, klass, faction=None, align=None) -> Optional[Dict]:
    data = path.read_bytes()
    if data[:8] != b"CRE V1.2" or len(data) < CRE_DIALOG + 8:
        return None
    name_ref, = struct.unpack_from("<i", data, CRE_NAME)
    anim_id, = struct.unpack_from("<I", data, CRE_ANIMATION)
    entry = tlk.get(name_ref)
    return {
        "cre": path.stem.upper(),
        "name": entry.text.strip() if entry else "",
        "gender": GENDERS.get(data[CRE_GENDER], "unknown"),
        "race": race.get(data[CRE_RACE], "").replace("_", " ").title(),
        "class": klass.get(data[CRE_CLASS], "").replace("_", " ").title(),
        "animation": clean_animation(anim.get(anim_id, f"0x{anim_id:04X}")),
        "faction": _ids_label((faction or {}).get(data[CRE_FACTION], ""), {"Unaligned", "Unknown"}),
        "alignment": _ids_label((align or {}).get(data[CRE_ALIGNMENT], ""), {"None"}),
        "areas": [],
        "dialog": data[CRE_DIALOG:CRE_DIALOG + 8].split(b"\x00", 1)[0].decode("ascii", "replace").upper(),
    }


def _ids_label(name: str, empty: set) -> str:
    """'LAWFUL_GOOD' -> 'Lawful Good'; '' for placeholder values. Fixes FACTION.IDS's 'HATMONIUM' typo."""
    label = name.replace("_", " ").title().replace("Hatmonium", "Harmonium").replace("Indep", "Indep (Free League)")
    return "" if label in empty else label


SHE_RE = re.compile(r"\b(she|her|hers|herself)\b", re.I)
HE_RE = re.compile(r"\b(he|him|his|himself)\b", re.I)
QUOTE_RE = re.compile(r'"[^"]*"')
FEMALE_ADDRESS = re.compile(r"\b(ma'am|madam|lady|milady|woman|lass|girl|mother|sister|mistress|miss)\b", re.I)
MALE_ADDRESS = re.compile(r"\b(sir|lad|boy|fellow|old man|father|brother|mister|lord)\b", re.I)


def gender_clues(npc_texts: List[str], player_texts: List[str]) -> Dict[str, int]:
    """
    Counts that hint at the speaker's gender: he/she in the narration of the
    NPC's own lines (quotes removed), and how the player's replies address them.
    """
    narration = " ".join(QUOTE_RE.sub(" ", t) for t in npc_texts)
    replies = " ".join(player_texts)
    return {"narration_she": len(SHE_RE.findall(narration)), "narration_he": len(HE_RE.findall(narration)),
            "player_says_female": len(FEMALE_ADDRESS.findall(replies)),
            "player_says_male": len(MALE_ADDRESS.findall(replies))}


def best_gender(creature_genders: List[str], clues: Dict[str, int]) -> Tuple[str, str]:
    """
    (gender, where it came from). The creature file's gender wins unless the
    narration clearly says otherwise; with neither, assume male.
    """
    she, he = clues["narration_she"], clues["narration_he"]
    narrated = "female" if she >= 3 and she > 3 * he else "male" if he >= 3 and he > 3 * she else ""
    game = {g for g in creature_genders if g in ("male", "female")}
    if len(game) == 1:
        g = game.pop()
        if narrated and narrated != g:
            return narrated, f"narration ({she} she / {he} he) overrides game file ({g})"
        return g, "game file"
    if narrated:
        return narrated, f"narration ({she} she / {he} he)"
    if she > he:
        return "female", f"narration, weak ({she} she / {he} he)"
    if he > she:
        return "male", f"narration, weak ({she} she / {he} he)"
    return "male", "default (no gender in game files or narration)"


def dlg_replies(path: Path) -> List[int]:
    """Text strrefs of a DLG's player replies (transitions with the has-text flag)."""
    data = path.read_bytes()
    if len(data) < 0x30 or data[:4] != b"DLG ":
        return []
    num_trans, trans_off = struct.unpack_from("<II", data, 0x10)
    out = []
    for t in range(num_trans):
        rec = trans_off + t * 32
        if rec + 8 > len(data):
            break
        flags, text = struct.unpack_from("<Ii", data, rec)
        if flags & 1 and text >= 0:
            out.append(text)
    return out


def area_names(ids_dir: Path, tlk) -> Dict[str, str]:
    """AREA resref -> map name, from MAPNAME.2DA."""
    path = ids_dir / "MAPNAME.2DA"
    names: Dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="latin-1").splitlines()[3:]:
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit() and tlk.get(int(parts[1])):
                names[parts[0].upper()] = tlk[int(parts[1])].text.strip()
    return names


def dlg_lines(path: Path) -> Optional[List[int]]:
    """SAY strrefs of a DLG V1.0, in state order (player replies are transitions, not read)."""
    data = path.read_bytes()
    if len(data) < 0x30 or data[:4] != b"DLG ":
        return None
    num_states, state_off = struct.unpack_from("<II", data, 0x08)
    out = []
    for i in range(num_states):
        rec = state_off + i * 16
        if rec + 4 > len(data):
            break
        out.append(struct.unpack_from("<i", data, rec)[0])
    return out


def voiced_names() -> Dict[str, str]:
    """Lower-cased speaker name -> voice they already use."""
    names = {p.stem.rsplit(" ", 1)[0]: p.stem.rsplit(" ", 1)[0] for p in VOICED_DIR.glob("*.txt")}
    if ALIASES_PATH.exists():
        names.update({k: v for k, v in json.loads(ALIASES_PATH.read_text(encoding="utf-8")).items()
                      if not k.startswith("_")})
    names.pop("Narrator", None)
    return {k.lower(): v for k, v in names.items()}


def build(extract_dir: Path) -> List[Dict]:
    tlk = parse_dialog_tlk(find_dialog_tlk(Path(cfg.GAME_DIRECTORY)))
    ids_dir = ensure_extracted(extract_dir, "ids", r".*\.ids", "ANIMATE.IDS")
    ensure_extracted(extract_dir, "ids", r".*\.2da", "MAPNAME.2DA")
    are_dir = ensure_extracted(extract_dir, "are", r".*\.are", "AR0300.ARE")
    anim, race, klass, faction, align = (read_ids(ids_dir / f) for f in
                                         ("ANIMATE.IDS", "RACE.IDS", "CLASS.IDS", "FACTION.IDS", "ALIGNMEN.IDS"))
    maps = area_names(ids_dir, tlk)

    by_dialog: Dict[str, List[Dict]] = {}
    cres: Dict[str, Dict] = {}
    for cre_path in iter_files_ci(extract_dir, "cre"):
        cre = parse_cre(cre_path, tlk, anim, race, klass, faction, align)
        if cre:
            cres[cre["cre"]] = cre
            if cre["dialog"] and cre["dialog"] != "NONE":
                by_dialog.setdefault(cre["dialog"], []).append({**cre, "source": "creature"})

    # Area actors with their own dialog: the CRE's stats, the actor's dialog (and animation if set)
    actors = area_actors(are_dir)
    where: Dict[str, set] = {}
    for actor in actors:
        where.setdefault(actor["cre"], set()).add(maps.get(actor["area"], actor["area"]))
    for entry_list in by_dialog.values():
        for c in entry_list:
            c["areas"] = sorted(where.get(c["cre"], ()))
    for actor in actors:
        cre = cres.get(actor["cre"])
        dlg = actor["dialog"]
        if not cre or not dlg or dlg == "NONE":
            continue
        if any(c["cre"] == cre["cre"] for c in by_dialog.get(dlg, [])):
            continue
        entry = {**cre, "dialog": dlg, "source": f"area {actor['area']}",
                 "areas": sorted(where.get(cre["cre"], ()))}
        if actor["animation"]:
            entry["animation"] = clean_animation(anim.get(actor["animation"], cre["animation"]))
        if not entry["name"]:
            entry["name"] = actor["actor"]
        by_dialog.setdefault(dlg, []).append(entry)

    # RealName the dialog report assigned to each DLG (its SystemName column)
    report_names: Dict[str, Counter] = {}
    sounds: Dict[int, str] = {}
    with open(cfg.CSV_PATH, "r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["SystemName"] and r["RealName"]:
                report_names.setdefault(r["SystemName"].upper(), Counter())[r["RealName"]] += 1
            if r["SoundResRef"]:
                sounds[int(r["StrRef"])] = r["SoundResRef"]

    voiced = voiced_names()
    result = []
    for dlg_path in sorted(iter_files_ci(extract_dir, "dlg")):
        says = dlg_lines(dlg_path)
        if not says:
            continue
        dlg = dlg_path.stem.upper()
        lines, seen = [], set()
        for s in says:
            entry = tlk.get(s)
            if s < 0 or s in seen or entry is None or not entry.text.strip():
                continue
            seen.add(s)
            lines.append({"strref": s, "text": entry.text.strip(), "has_sound": bool(sounds.get(s))})
        if not lines:
            continue
        creatures = sorted(by_dialog.get(dlg, []), key=lambda c: c["cre"])
        names = Counter(c["name"] for c in creatures if c["name"])
        # Creature names win; the report's name is only a fallback (it mislabels e.g. merchants as "Dabus")
        report = report_names.get(dlg, Counter())
        if names:
            speaker = next((n for n, _ in report.most_common() if n in names), names.most_common(1)[0][0])
        else:
            speaker = report.most_common(1)[0][0] if report else ""
        # The player's own CREs point at all sorts of dialogs, so they never mark one as voiced
        voice = next((voiced[n.lower()] for n in names if n.lower() in voiced and n != PLAYER_NAME), None)
        if voice is None and speaker.lower() in voiced and (not creatures or any(ln["has_sound"] for ln in lines)):
            voice = voiced[speaker.lower()]     # report name only counts if no creature contradicts it
        spoken = any('"' in ln["text"] for ln in lines)
        replies = [tlk[r].text for r in dlg_replies(dlg_path) if tlk.get(r)]
        clues = gender_clues([ln["text"] for ln in lines], replies)
        gender, gender_source = best_gender([c["gender"] for c in creatures], clues)
        result.append({
            "dialog": dlg,
            "speaker": speaker,
            "creatures": creatures,
            "genders": sorted({c["gender"] for c in creatures}),
            "animations": sorted({c["animation"] for c in creatures}),
            "gender_clues": clues,
            "gender": gender,
            "gender_source": gender_source,
            "already_voiced": voice is not None,
            "narration_only": not spoken,
            "existing_voice": voice or "",
            "line_count": len(lines),
            "voiced_line_count": sum(ln["has_sound"] for ln in lines),
            "lines": lines,
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--extract-dir", type=Path, default=Path(cfg.EXTRACT_DIR))
    args = parser.parse_args()
    npcs = build(args.extract_dir)
    CAST_DIR.mkdir(exist_ok=True)
    NPCS_PATH.write_text(json.dumps(npcs, indent=2, ensure_ascii=False), encoding="utf-8")
    todo = [n for n in npcs if not n["already_voiced"]]
    print(f"Wrote {NPCS_PATH}: {len(npcs)} dialogs, {len(todo)} need a voice "
          f"({sum(n['line_count'] for n in todo)} lines), "
          f"{sum(1 for n in npcs if n['creatures'])} with a known creature")


if __name__ == "__main__":
    main()
