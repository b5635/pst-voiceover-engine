#!/usr/bin/env python3
"""
WoW Voice Library Downloader
Downloads World of Warcraft NPC greeting / farewell / vendor / "pissed" voice
lines (one folder per race, gender and archetype) into a labelled voice
library, so an LLM can cast unvoiced Planescape: Torment NPCs.

Files are fetched from wago.tools' CASC mirror by FileDataID, found through the
community listfile that E:/GitHub/RetailSounds already keeps (same approach as
RetailSounds/tools/build_data.py).

    voice_library/wow/<group>/<file>.wav + .txt    every line, 24 kHz mono
    voice_library/wow/<group>/reference.wav + .txt longest lines joined (~8-15 s)
    voice_library/wow/catalog.json                 gender, race, accent, description

WoW lines have no transcripts: .txt files stay empty until
voicelib_transcribe.py (Whisper) fills them in.

Usage:
    python voicelib_wow.py                 # download + build
    python voicelib_wow.py --no-download   # rebuild catalog/references only
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# ==================== CONFIGURATION ====================
# RetailSounds checkout data folder (community-listfile.csv + manifest.json); --retailsounds or env overrides
RETAILSOUNDS_DATA = Path(os.environ.get("RETAILSOUNDS_DATA", r"E:/GitHub/RetailSounds/data"))
CAST_FILES = [Path("pst_cast/casting.json"), Path("pst-cast-overrides.json")]   # where cast voice ids live
OUT_DIR = Path("voice_library/wow")
CACHE_DIR = Path("voice_library/_cache/wow_ogg")
AGENT = {"User-Agent": "voice-library-tools"}
LINE_RE = re.compile(r"(greet|farewell|vendor|pissed)", re.I)
# "Pissed" (clicked too often) and "aggro" lines are taunts/threats: kept on disk, never in reference.wav
REFERENCE_EXCLUDE_RE = re.compile(r"(pissed|aggro)", re.I)
MAX_FILES_PER_GROUP = 24
SAMPLE_RATE = 24000
GAP_SECONDS = 0.35
REFERENCE_MAX_SECONDS = 15.0
REFERENCE_TARGET_SECONDS = 11.0

# How each WoW race sounds, for the LLM
ACCENTS = {
    "Human": "plain standard English", "Dwarf": "thick Scottish brogue, hearty",
    "Gnome": "high-pitched, quick, squeaky", "Night Elf": "calm, airy, ethereal",
    "Orc": "deep, gravelly, guttural", "Tauren": "very deep, slow, earthy, Native-American cadence",
    "Troll": "Caribbean / Jamaican lilt", "Undead": "raspy, hollow, sardonic, decayed",
    "Goblin": "fast-talking, nasal, New York huckster", "Blood Elf": "refined, haughty, aristocratic",
    "Draenei": "Eastern-European accent, measured", "Worgen": "British, Victorian, gruff",
    "Pandaren": "warm, gentle, East-Asian cadence", "Ethereal": "echoing, otherworldly merchant",
    "Broken": "rough, wounded Draenei accent", "Kul Tiran": "salty British seafarer",
    "Nightborne": "elegant, decadent, aristocratic elven", "Dracthyr": "draconic, resonant",
    "Dark Iron Dwarf": "fiery, coarse Scottish", "Highmountain Tauren": "deep, Native-American cadence",
    "Vulpera": "small, raspy, quick desert fox-folk", "Naga": "hissing, serpentine",
    "Arakkoa": "shrill, avian, screechy", "Kyrian": "serene, angelic, resonant",
    "Kobold": "squeaky, simple, childish", "Gnoll": "yappy, hyena-like", "Furbolg": "deep, gentle, bear-like",
}

# Vanilla generic NPC folders are "<race><gender><archetype>npc"
VANILLA_RACES = {"human": "Human", "dwarf": "Dwarf", "gnome": "Gnome", "nightelf": "Night Elf", "orc": "Orc",
                 "tauren": "Tauren", "troll": "Troll", "undead": "Undead", "goblin": "Goblin"}
ARCHETYPES = {
    "standard": "ordinary commoner", "guard": "town guard", "warrior": "warrior / soldier",
    "official": "official / noble", "shaman": "shaman, spiritual", "priestess": "priestess, serene",
    "sentinel": "sentinel soldier", "elder": "elder, wise and slow", "dark": "dark, sinister",
    "magic": "spellcaster", "shady": "shady rogue", "grim": "grim, dour", "maternal": "motherly, kind",
    "young": "young, eager", "happy": "cheerful", "nerdy": "nerdy, fussy", "zany": "zany, manic",
    "gruff": "gruff", "laidback": "laid-back, relaxed", "old": "old, creaky",
}

# Later expansions use one-off folder names: folder -> (gender, race, description)
EXTRA_GROUPS: Dict[str, Tuple[str, str, str]] = {
    "npcbloodelfmalestandard": ("male", "Blood Elf", "ordinary commoner"),
    "npcbloodelfmalenoble": ("male", "Blood Elf", "noble, snobbish"),
    "npcbloodelfmalemilitary": ("male", "Blood Elf", "soldier"),
    "npcbloodelffemalestandard": ("female", "Blood Elf", "ordinary commoner"),
    "npcbloodelffemalenoble": ("female", "Blood Elf", "noble, snobbish"),
    "npcbloodelffemalemilitary": ("female", "Blood Elf", "soldier"),
    "npcdraeneimalestandard": ("male", "Draenei", "ordinary commoner"),
    "npcdraeneimalenoble": ("male", "Draenei", "noble, dignified"),
    "npcdraeneimalemilitary": ("male", "Draenei", "soldier"),
    "npcdraeneifemalestandard": ("female", "Draenei", "ordinary commoner"),
    "npcdraeneifemalenoble": ("female", "Draenei", "noble, dignified"),
    "npcdraeneifemalemilitary": ("female", "Draenei", "soldier"),
    "worgencivm": ("male", "Worgen", "civilian"), "worgencivf": ("female", "Worgen", "civilian"),
    "worgenguardm": ("male", "Worgen", "guard"), "worgenguardf": ("female", "Worgen", "guard"),
    "worgenvenm": ("male", "Worgen", "merchant"), "worgenvenf": ("female", "Worgen", "merchant"),
    "goblincivm": ("male", "Goblin", "civilian"), "goblincivf": ("female", "Goblin", "civilian"),
    "goblinvendorm": ("male", "Goblin", "merchant, salesman"), "goblinvendorf": ("female", "Goblin", "merchant"),
    "goblinguardm": ("male", "Goblin", "guard / bruiser"), "goblinguardf": ("female", "Goblin", "guard"),
    "pandarencivmstandard": ("male", "Pandaren", "civilian"), "pandarencivf": ("female", "Pandaren", "civilian"),
    "pandarencivmold": ("male", "Pandaren", "old man, wise"), "pandarencivfold": ("female", "Pandaren", "old woman"),
    "pandarencivmgruff": ("male", "Pandaren", "gruff"), "pandarencivfgruff": ("female", "Pandaren", "gruff"),
    "pandarenmalechild": ("male", "Pandaren", "child"), "pandarenfemalechild": ("female", "Pandaren", "child"),
    "etherealservicenpc1": ("male", "Ethereal", "merchant, mysterious"),
    "etherealservicenpc2": ("male", "Ethereal", "merchant, mysterious"),
    "etherealservicenpc3": ("male", "Ethereal", "merchant, mysterious"),
    "brokennpc1": ("male", "Broken", "outcast, weary"), "brokennpc2": ("male", "Broken", "outcast, bitter"),
    "kultiranmale": ("male", "Kul Tiran", "big sailor, hearty"),
    "kultiranfemale": ("female", "Kul Tiran", "sailor, hearty"),
    "nightbornemale": ("male", "Nightborne", "aristocrat"),
    "dracthyrmale": ("male", "Dracthyr", "dragon-kin soldier"),
    "dracthyrfemale": ("female", "Dracthyr", "dragon-kin soldier"),
    "darkirondwarfmale": ("male", "Dark Iron Dwarf", "fiery, aggressive"),
    "highmountain_civilian_male": ("male", "Highmountain Tauren", "civilian"),
    "vulpera_generic_female_merchant": ("female", "Vulpera", "merchant"),
    "vulpera_military_guard_m_horde": ("male", "Vulpera", "guard"),
    "genericnagamale": ("male", "Naga", "serpent-man, menacing"),
    "genericarakkoaoutcast": ("male", "Arakkoa", "bird-man outcast, mad"),
    "kyrianfemale": ("female", "Kyrian", "angelic servant"),
    "koboldchild": ("male", "Kobold", "small creature, childish"),
    "gnollkid": ("male", "Gnoll", "young gnoll, yappy"),
    "furbolg": ("male", "Furbolg", "bear-folk, gentle"),
}
# =======================================================


def load_listfile() -> Dict[str, List[Tuple[int, str]]]:
    """folder name -> [(FileDataID, file name)] for sound/creature/<folder>/*.ogg."""
    groups: Dict[str, List[Tuple[int, str]]] = {}
    with open(RETAILSOUNDS_DATA / "community-listfile.csv", encoding="utf-8", errors="replace") as f:
        for line in f:
            fdid, _, path = line.rstrip("\n").partition(";")
            if not path.startswith("sound/creature/") or not path.endswith(".ogg"):
                continue
            parts = path.split("/")
            if len(parts) != 4:
                continue
            groups.setdefault(parts[2], []).append((int(fdid), parts[3]))
    return groups


def describe_group(folder: str) -> Tuple[str, str, str]:
    """(gender, race, archetype description) for a voice folder."""
    if folder in EXTRA_GROUPS:
        return EXTRA_GROUPS[folder]
    m = re.fullmatch(r"([a-z]+?)(male|female)([a-z]*)npc", folder)
    race = VANILLA_RACES[m.group(1)]
    return m.group(2), race, ARCHETYPES.get(m.group(3), m.group(3))


def wanted_groups(listfile: Dict[str, List[Tuple[int, str]]]) -> List[str]:
    vanilla = [g for g in listfile if re.fullmatch(r"(%s)(male|female)[a-z]*npc" % "|".join(VANILLA_RACES), g)]
    return sorted(vanilla) + [g for g in EXTRA_GROUPS if g in listfile]


def _voice_ids(node) -> Iterable[str]:
    """Every "voice_id" / "voice" string anywhere in a casting or override JSON tree."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("voice_id", "voice") and isinstance(v, str):
                yield v
            else:
                yield from _voice_ids(v)
    elif isinstance(node, list):
        for v in node:
            yield from _voice_ids(v)


def cast_groups(paths: Iterable[Path] = CAST_FILES) -> Set[str]:
    """WoW folders some PST line is cast with (pst_cast/casting.json + hand-picked overrides)."""
    groups: Set[str] = set()
    for path in paths:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            groups |= {v[4:] for v in _voice_ids(data) if v.lower().startswith("wow:")}
    return groups


def download(fdid: int, dest: Path, build: str) -> bool:
    if dest.exists():
        return True
    url = f"https://wago.tools/api/casc/{fdid}?version={build}&download"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=AGENT)
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            if not data:
                return False        # wago.tools serves encrypted/unpublished files as empty: don't retry
            if data[:4] != b"OggS":
                raise ValueError("not an ogg file")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return True
        except Exception as e:
            print(f"  {fdid} {dest.name}: {e}")
            time.sleep(2 * (attempt + 1))
    return False


def to_wav(src: Path, dst: Path) -> bool:
    if dst.exists():
        return True
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                        "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16", str(dst)], capture_output=True)
    return r.returncode == 0


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def build_reference(folder: Path) -> Tuple[str, float]:
    """Join the longest lines into reference.wav/.txt; returns (text, seconds)."""
    clips = []
    for wav in sorted(folder.glob("*.wav")):
        if wav.stem == "reference" or REFERENCE_EXCLUDE_RE.search(wav.stem):
            continue
        txt = wav.with_suffix(".txt")
        clips.append((wav, txt.read_text(encoding="utf-8").strip() if txt.exists() else "", wav_seconds(wav)))
    # Prefer transcribed, longer clips; fill toward the target length
    clips.sort(key=lambda c: (not c[1], -c[2]))
    picked, total = [], 0.0
    for c in clips:
        if c[2] < 0.8 or total + c[2] > REFERENCE_MAX_SECONDS:
            continue
        picked.append(c)
        total += c[2] + GAP_SECONDS
        if total >= REFERENCE_TARGET_SECONDS:
            break
    gap = b"\x00\x00" * int(SAMPLE_RATE * GAP_SECONDS)
    with wave.open(str(folder / "reference.wav"), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        for i, (wav, _t, _s) in enumerate(picked):
            with wave.open(str(wav), "rb") as w:
                if i:
                    out.writeframes(gap)
                out.writeframes(w.readframes(w.getnframes()))
    text = " ".join(t for _w, t, _s in picked if t) if all(t for _w, t, _s in picked) else ""
    (folder / "reference.txt").write_text(text, encoding="utf-8")
    return text, round(wav_seconds(folder / "reference.wav"), 1)


def build_catalog(groups: List[str], rebuild_references: bool = False) -> List[Dict]:
    """
    Catalog entries for every group with at least 3 usable clips.

    A finished reference (reference.wav + a non-empty reference.txt) is kept as is:
    Drama 3 voices are compiled from it, so it only changes with rebuild_references.
    """
    catalog = []
    for g in groups:
        folder = OUT_DIR / g
        wavs = [w for w in folder.glob("*.wav") if w.stem != "reference"
                and not REFERENCE_EXCLUDE_RE.search(w.stem)] if folder.is_dir() else []
        if len(wavs) < 3:
            continue
        ref_wav, ref_txt = folder / "reference.wav", folder / "reference.txt"
        kept = (not rebuild_references and ref_wav.exists() and ref_txt.exists()
                and ref_txt.read_text(encoding="utf-8").strip())
        if kept:
            text, seconds = ref_txt.read_text(encoding="utf-8").strip(), round(wav_seconds(ref_wav), 1)
        else:
            text, seconds = build_reference(folder)
        gender, race, arche = describe_group(g)
        lines = [w.with_suffix(".txt").read_text(encoding="utf-8").strip()
                 for w in sorted(wavs) if w.with_suffix(".txt").exists()]
        catalog.append({
            "id": f"wow:{g}",
            "source": "World of Warcraft",
            "name": f"{race} {gender} {arche}",
            "gender": gender,
            "race": race,
            "category": "npc_generic",
            "description": f"{race}, {gender.capitalize()}, {arche}; voice: {ACCENTS.get(race, '')}",
            "lines": len(wavs),
            "reference_wav": (folder / "reference.wav").as_posix(),
            "reference_text": text,
            "reference_seconds": seconds,
            "sample_lines": [t for t in lines if t][:6],
        })
    return catalog


def main() -> None:
    global RETAILSOUNDS_DATA
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-download", action="store_true", help="Only rebuild references and catalog")
    parser.add_argument("--only", action="append", help="Only download this folder (repeatable)")
    parser.add_argument("--cast-only", action="store_true",
                        help="Only download the folders PST casting uses (pst_cast/casting.json + overrides)")
    parser.add_argument("--retailsounds", type=Path, help=f"RetailSounds data folder (default {RETAILSOUNDS_DATA})")
    parser.add_argument("--rebuild-references", action="store_true",
                        help="Rebuild reference.wav/.txt even when finished (changes Drama 3 clones)")
    args = parser.parse_args()

    if args.retailsounds:
        RETAILSOUNDS_DATA = args.retailsounds
    if not (RETAILSOUNDS_DATA / "community-listfile.csv").exists():
        sys.exit(f"No community-listfile.csv in {RETAILSOUNDS_DATA}: pass --retailsounds <RetailSounds>/data")

    listfile = load_listfile()
    all_groups = wanted_groups(listfile)
    groups = all_groups
    if args.only:
        groups = [g for g in groups if g in args.only]
    if args.cast_only:
        cast = cast_groups()
        unknown = sorted(cast - set(all_groups))
        if unknown:
            print(f"Cast but not in the listfile (skipped): {', '.join(unknown)}")
        groups = [g for g in groups if g in cast]
    build = json.loads((RETAILSOUNDS_DATA / "manifest.json").read_text())["retail_build"]
    print(f"{len(groups)} of {len(all_groups)} voice groups to fetch, retail build {build}")

    if not args.no_download:
        jobs = []
        for g in groups:
            files = sorted((n, f) for f, n in listfile[g] if LINE_RE.search(n))[:MAX_FILES_PER_GROUP]
            jobs += [(g, fdid, name) for name, fdid in files]
        print(f"{len(jobs)} files")

        def fetch(job):
            g, fdid, name = job
            ogg = CACHE_DIR / g / name
            wav = OUT_DIR / g / (Path(name).stem + ".wav")
            if wav.exists():
                return              # already in the library: no need for the ogg again
            wav.parent.mkdir(parents=True, exist_ok=True)
            if download(fdid, ogg, build) and to_wav(ogg, wav):
                txt = wav.with_suffix(".txt")
                if not txt.exists():
                    txt.write_text("", encoding="utf-8")

        with ThreadPoolExecutor(max_workers=4) as pool:
            for i, _ in enumerate(pool.map(fetch, jobs), 1):
                if i % 100 == 0:
                    print(f"  {i}/{len(jobs)}")

    # The catalog always covers the whole library on disk, never just what was fetched this run
    catalog = build_catalog(all_groups, args.rebuild_references)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "catalog.json").write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    untranscribed = sum(1 for v in catalog if not v["reference_text"])
    print(f"Wrote {len(catalog)} voices to {OUT_DIR}; {untranscribed} still need voicelib_transcribe.py")


if __name__ == "__main__":
    main()
