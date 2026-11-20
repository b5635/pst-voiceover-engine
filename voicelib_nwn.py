#!/usr/bin/env python3
"""
NWN Voice Library Extractor
Pulls the speaking soundsets out of Neverwinter Nights (Enhanced Edition) into a
labelled voice library, so an LLM can cast unvoiced Planescape: Torment NPCs.

For every soundset in soundset.2da with enough spoken lines (monster sets that
only grunt, roar and battlecry are skipped), this writes:

    voice_library/nwn/<Label>/<NN Slot>.wav + .txt   every spoken line, 24 kHz mono
    voice_library/nwn/<Label>/reference.wav + .txt   the longest lines joined
                                                     (~8-15 s) for voice cloning

and an entry in voice_library/nwn/catalog.json with name, gender, race,
category and a short description built from the soundset's own name string
(e.g. "Human, Male, Older, World-Weary").

Usage:
    python voicelib_nwn.py                     # default NWN path
    python voicelib_nwn.py --nwn "E:/.../Neverwinter Nights" --min-lines 6
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from libs.aurora import RES_2DA, RES_SSF, RES_WAV, SSF_SLOTS, ResourceIndex, read_2da, read_ssf, read_tlk, wav_payload

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# ==================== CONFIGURATION ====================
DEFAULT_NWN_DIR = Path(r"E:/SteamLibrary/steamapps/common/Neverwinter Nights")
KEYS = ["data/nwn_base.key", "data/nwn_retail.key"]
OUT_DIR = Path("voice_library/nwn")
MIN_SPEECH_LINES = 6            # fewer spoken lines = monster set, skipped
# Only these slots go into reference.wav: every other slot (battle cries, taunts,
# pain, threats...) has a tone or aggression baked in that the clone would copy
REFERENCE_SLOTS = ["Hello", "Goodbye", "Something To Say"]
EXCLUDED_SETS = {"AsabiThrall"}     # never offered for casting
SAMPLE_RATE = 24000
GAP_SECONDS = 0.35

# Slots that are noises, not words
NON_SPEECH_SLOTS = {"Attack Grunt 1", "Attack Grunt 2", "Attack Grunt 3", "Pain Grunt 1", "Pain Grunt 2",
                    "Pain Grunt 3", "Near Death", "Death", "Laugh", "Poisoned"}

# soundset.2da TYPE column
CATEGORIES = {"0": "player", "1": "henchman", "2": "npc_full", "3": "npc_partial", "4": "creature"}

RACES = ["Half-Orc", "Half-Elf", "Halfelf", "Human", "Elf", "Dwarf", "Gnome", "Halfling", "Child", "Drow",
         "Duergar", "Avariel", "Orc", "Goblin", "Kobold", "Lizardfolk", "Yuan-ti", "Medusa", "Minotaur",
         "Dragon", "Lich", "Wraith", "Imp", "Succubus", "Celestial", "Spirit", "Nymph", "Fairy", "Mephit",
         "Golem", "Devil", "Genie", "Ogre", "Deva"]

# Named NWN characters whose soundset name says nothing about them
NAMED: Dict[str, Tuple[str, str, str]] = {   # label -> (gender, race, description)
    "AvHen1F": ("female", "Elf", "Linu La'neral, earnest elven cleric of Sehanine, gentle and devout"),
    "AvHen2F": ("female", "Human", "Sharwyn, confident human bard, warm and theatrical"),
    "AvHen3M": ("male", "Half-Orc", "Daelan Red Tiger, deep-voiced half-orc barbarian, calm and honourable"),
    "AvHen4M": ("male", "Halfling", "Tomi 'Grin' Undergallows, cheeky halfling rogue, quick and playful"),
    "AvHen5M": ("male", "Dwarf", "Grimgnaw, grim dwarven monk, gravelly and dour"),
    "AvHen6M": ("male", "Gnome", "Boddyknock Glinckle, excitable gnome wizard, nasal and fussy"),
    "AribetF": ("female", "Elf", "Aribeth, noble elven paladin, proud and earnest"),
    "Aribeth_XP2_PALADIN": ("female", "Elf", "Aribeth, noble elven paladin, proud and earnest"),
    "Aribeth_XP2_BLACKGUARD": ("female", "Elf", "Aribeth fallen to blackguard, bitter and cold"),
    "HaedraF": ("female", "Elf", "Haedraline, elven agent, poised and serious"),
    "MaugriM": ("male", "Human", "Maugrim, sinister cleric, menacing and smooth"),
    "MoragF": ("female", "Reptilian", "Morag, ancient reptilian queen, regal and hissing"),
    "NasherM": ("male", "Human", "Lord Nasher, aging king, commanding and gruff"),
    "FenthiM": ("male", "Human", "Fenthick, young cleric, kind and earnest"),
    "DestheM": ("male", "Human", "Desther, cleric turned traitor, sly and cold"),
    "GendM": ("male", "Human", "Aarin Gend, spymaster, dry and measured"),
    "Deekin_Scalesinger": ("male", "Kobold", "Deekin, kobold bard, squeaky, enthusiastic, speaks in third person"),
    "Deekin_XP2": ("male", "Kobold", "Deekin, kobold bard, squeaky, enthusiastic, speaks in third person"),
    "Dorna_Trapspringer": ("female", "Dwarf", "Dorna Trapspringer, brash dwarven rogue, loud and boisterous"),
    "Drogan_Droganson": ("male", "Dwarf", "Drogan, old dwarven master, warm and wise"),
    "Xanos": ("male", "Half-Orc", "Xanos, arrogant half-orc sorcerer, sneering"),
    "Heurodis": ("female", "Human", "Heurodis, gentle noblewoman"),
    "JNah": ("female", "Human", "J'Nah, desert sorceress, haughty"),
    "Katriana": ("female", "Human", "Katriana, scheming merchant, sly"),
    "Mischa_Waymeet": ("female", "Human", "Mischa, young warrior, defiant"),
    "Valen_Shadowbreath": ("male", "Tiefling", "Valen Shadowbreath, tiefling warrior, intense and brooding"),
    "Nathyrra": ("female", "Drow", "Nathyrra, drow assassin, cool and precise"),
    "Daelan_XP2": ("male", "Half-Orc", "Daelan Red Tiger, deep-voiced half-orc barbarian, calm and honourable"),
    "Linu_XP2": ("female", "Elf", "Linu La'neral, earnest elven cleric, gentle"),
    "Sharwyn_XP2": ("female", "Human", "Sharwyn, confident human bard, warm"),
    "Tomi_XP2": ("male", "Halfling", "Tomi Undergallows, cheeky halfling rogue"),
    "Durnan": ("male", "Human", "Durnan, gruff old innkeeper, terse"),
    "Halaster": ("male", "Human", "Halaster, ancient mad archmage, erratic and cackling"),
    "Devil_Big": ("male", "Devil", "Mephistopheles, archdevil, booming and silky"),
    "Mephisopheles": ("male", "Devil", "Mephistopheles, archdevil, booming and silky"),
    "Drow_Valsharess": ("female", "Drow", "The Valsharess, drow queen, imperious and cruel"),
    "The_Genie": ("male", "Genie", "genie, booming and theatrical"),
    "Legendary_Weapon": ("male", "Spirit", "intelligent weapon, hollow and eerie"),
    "Sensei_Dharvana": ("female", "Human", "Sensei Dharvana, monk teacher, calm and disciplined"),
    "The_GateKeeper": ("male", "Spirit", "otherworldly gatekeeper, solemn"),
    "Sleeping_Man": ("male", "Human", "dreaming man, drowsy and distant"),
    "Knower_of_Places_": ("female", "Spirit", "planar oracle, airy and cryptic"),
    "Knower_of_Names_": ("female", "Spirit", "planar oracle, airy and cryptic"),
    "Ashtara": ("female", "Lizardfolk", "Ashtara, reptilian priestess, hissing"),
    "AsabiThrall": ("male", "Lizardfolk", "Asabi thrall, hissing reptile soldier"),
    "Ayala": ("female", "Human", "Ayala, commoner"),
    "Mhaere": ("female", "Human", "Mhaere, commoner"),
    "White_Thesta": ("female", "Human", "White Thesta, villager"),
    "Grovel": ("male", "Goblin", "Grovel, cringing goblin"),
    "The_Fairy_Queen": ("female", "Fairy", "Fairy Queen, light and lilting"),
    "The_Ogre_Boss": ("male", "Ogre", "ogre chieftain, dim and booming"),
    "Arden_Swift": ("male", "Human", "Arden Swift, rogue"),
    "Quarry_Boss": ("male", "Human", "quarry boss, rough"),
    "Drow_Seer": ("female", "Drow", "drow seer, cryptic"),
    "Drow_Imloth": ("male", "Drow", "drow priest, sinister"),
    "Drow_Matron2": ("female", "Drow", "drow matron, imperious"),
    "Boatman": ("male", "Human", "boatman, laconic"),
    "Slave_Leader": ("male", "Human", "slave leader, defiant"),
    "Agarly": ("female", "Human", "commoner"),
    "Berger": ("male", "Human", "commoner"),
    "Sobrey": ("male", "Human", "commoner"),
    "Armand": ("male", "Human", "commoner"),
    "Deva": ("female", "Celestial", "deva, radiant and serene"),
    "Assam": ("male", "Human", "desert warrior, proud"),
    "Wizard": ("male", "Human", "blackguard, menacing"),
    "Golem_Good": ("male", "Golem", "Golem, Metal"),
}
# =======================================================


def ffmpeg_to_pcm(src: Path, dst: Path) -> bool:
    """Decode anything ffmpeg reads (PCM, ADPCM, MP3) to 24 kHz mono 16-bit WAV."""
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                             "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16", str(dst)],
                            capture_output=True)
    return result.returncode == 0 and dst.exists()


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def concat_wavs(parts: List[Path], dst: Path) -> None:
    """Join same-format PCM WAVs with a short silence between them."""
    gap = b"\x00\x00" * int(SAMPLE_RATE * GAP_SECONDS)
    with wave.open(str(dst), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        for i, p in enumerate(parts):
            with wave.open(str(p), "rb") as w:
                if i:
                    out.writeframes(gap)
                out.writeframes(w.readframes(w.getnframes()))


def is_speech(text: str) -> bool:
    """Spoken words, not '*grunt*' or '[laughs]' style placeholders."""
    t = text.strip()
    if not t or t.startswith(("*", "[", "<")):
        return False
    return len(re.findall(r"[A-Za-z]{2,}", t)) >= 1


def describe(label: str, name: str, gender_col: str) -> Tuple[str, str, str]:
    """(gender, race, description) from the soundset name string, e.g. 'Dwarf, Male, Veteran'."""
    if label in NAMED:
        return NAMED[label]
    parts = [p.strip() for p in name.split(",") if p.strip()]
    lower = [p.lower() for p in parts]
    gender = "female" if "female" in lower else "male" if "male" in lower else (
        "female" if gender_col == "1" else "male")
    race = ""
    for p in parts:
        for r in RACES:
            if p.lower().startswith(r.lower()):
                race = {"Halfelf": "Half-Elf", "Child": "Human"}.get(r, r)
                break
        if race:
            break
    return gender, race, name


def extract_set(idx: ResourceIndex, tlk: List[str], row: Dict[str, str], min_lines: int,
                tmp: Path) -> Optional[Dict]:
    """Extract one soundset; None if it has too few spoken lines."""
    label = row["LABEL"]
    if label in EXCLUDED_SETS:
        return None
    data = idx.get(row["RESREF"], RES_SSF)
    if data is None:
        return None
    lines = []
    for n, (slot, (res, strref)) in enumerate(zip(SSF_SLOTS, read_ssf(data)), 1):
        if slot in NON_SPEECH_SLOTS or not res or not (0 <= strref < len(tlk)):
            continue
        text = tlk[strref].strip()
        if not is_speech(text):
            continue
        wav = idx.get(res, RES_WAV)
        if wav is None:
            continue
        lines.append((n, slot, res, text, wav))
    if len(lines) < min_lines:
        return None

    out = OUT_DIR / re.sub(r"[^\w\-]", "_", label)
    out.mkdir(parents=True, exist_ok=True)
    clips = []
    for n, slot, res, text, wav in lines:
        raw = tmp / f"{label}_{res}.bin"
        raw.write_bytes(wav_payload(wav))
        dst = out / f"{n:02d} {slot}.wav"
        if ffmpeg_to_pcm(raw, dst):
            (out / f"{n:02d} {slot}.txt").write_text(text, encoding="utf-8")
            clips.append({"slot": slot, "file": dst.name, "text": text, "seconds": round(wav_seconds(dst), 2)})
        raw.unlink(missing_ok=True)
    if len(clips) < min_lines:
        shutil.rmtree(out, ignore_errors=True)
        return None

    # Reference: the neutral slots only (greeting, farewell, "come talk to me")
    by_slot = {c["slot"]: c for c in clips}
    picked = [by_slot[s] for s in REFERENCE_SLOTS if s in by_slot]
    if not picked:
        shutil.rmtree(out, ignore_errors=True)
        return None
    for stale in out.glob("reference.*"):
        stale.unlink()
    concat_wavs([out / c["file"] for c in picked], out / "reference.wav")
    (out / "reference.txt").write_text(" ".join(c["text"] for c in picked), encoding="utf-8")

    name = tlk[int(row["STRREF"])].strip() if row.get("STRREF", "").isdigit() else label
    gender, race, desc = describe(label, name, row.get("GENDER", ""))
    category = CATEGORIES.get(row.get("TYPE", ""), "other")
    return {
        "id": f"nwn:{label}",
        "source": "Neverwinter Nights",
        "name": name,
        "gender": gender,
        "race": race,
        "category": category,
        "description": desc,
        "lines": len(clips),
        "reference_wav": str((out / "reference.wav").as_posix()),
        "reference_text": (out / "reference.txt").read_text(encoding="utf-8"),
        "reference_seconds": round(wav_seconds(out / "reference.wav"), 1),
        "sample_lines": [c["text"] for c in clips[:6]],
        "clips": clips,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--nwn", type=Path, default=DEFAULT_NWN_DIR, help="Neverwinter Nights install folder")
    parser.add_argument("--min-lines", type=int, default=MIN_SPEECH_LINES, help="Skip sets with fewer spoken lines")
    parser.add_argument("--only", action="append", help="Only this soundset LABEL (repeatable)")
    args = parser.parse_args()

    missing = [k for k in KEYS + ["lang/en/data/dialog.tlk"] if not (args.nwn / k).exists()]
    if missing:
        sys.exit(f"{args.nwn} doesn't look like a Neverwinter Nights: Enhanced Edition install "
                 f"(missing {', '.join(missing)}). Pass the folder that holds data/ and lang/.")

    idx = ResourceIndex(args.nwn, KEYS, [args.nwn / "ovr"])
    tlk = read_tlk(args.nwn / "lang" / "en" / "data" / "dialog.tlk")
    rows = [r for r in read_2da(idx.get("soundset", RES_2DA)) if r.get("RESREF")]
    if args.only:
        rows = [r for r in rows if r["LABEL"] in args.only]
    print(f"{len(rows)} soundsets in soundset.2da")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda r: extract_set(idx, tlk, r, args.min_lines, Path(tmp)), rows))
    catalog = [r for r in results if r]
    # De-duplicate soundsets that share the same voice files (e.g. XP2 copies)
    seen, unique = set(), []
    for v in catalog:
        # Same audio = same voice files (short neutral references can share text, so compare bytes)
        key = Path(v["reference_wav"]).read_bytes()
        if key in seen:
            shutil.rmtree(Path(v["reference_wav"]).parent, ignore_errors=True)
            continue
        seen.add(key)
        unique.append(v)
    catalog_path = OUT_DIR / "catalog.json"
    if args.only and catalog_path.exists():
        # Partial run: replace just these soundsets in the existing catalog, keep the rest
        fresh = {v["id"]: v for v in unique}
        asked = {f"nwn:{label}" for label in args.only}
        old = json.loads(catalog_path.read_text(encoding="utf-8"))
        merged = [fresh.pop(v["id"], v) for v in old if v["id"] in fresh or v["id"] not in asked]
        unique = merged + list(fresh.values())
    (OUT_DIR / "catalog.json").write_text(json.dumps(unique, indent=2, ensure_ascii=False), encoding="utf-8")
    by_gender = {g: sum(v["gender"] == g for v in unique) for g in ("male", "female")}
    print(f"Wrote {len(unique)} voices to {OUT_DIR} ({by_gender['male']} male, {by_gender['female']} female)")


if __name__ == "__main__":
    main()
