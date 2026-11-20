#!/usr/bin/env python3
"""
PST Mood References
Builds per-mood voice references for every character with original
voice acting in Planescape: Torment.

Workflow (after dialog-report-prepare.py has produced cfg.CSV_PATH):

    python pst_moods.py collect   # extract every original voiced dialog clip + stats
    (label each clip's mood in pst-mood-labels.json - see the pst-voice-moods skill)
    python pst_moods.py status    # seconds of audio per character/mood
    python pst_moods.py build     # stitch clips into 8-10s references + moods.json

Only conversation lines count (combat/selection barks are ignored). Clips that
share a mood are joined at the original game sample rate (22.05 kHz); a
mood without enough audio is dropped, and its lines fall back to the
character's default mood. Drama 3 stacks those refs into one voice per character.
"""

import argparse
import csv
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from libs.appconfig import cfg
from libs.utils import setup_logging
from pst_revoice import NARRATOR, SAMPLE_RATE, is_original_sound, split_segments

# Ensure UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

logger = setup_logging(Path(__file__).stem)

# ==================== CONFIGURATION ====================
WORK_DIR = Path("pst_moods")
CLIPS_DIR = WORK_DIR / "clips"
CLIPS_CSV = WORK_DIR / "clips.csv"
LABELS_PATH = Path("pst-mood-labels.json")    # {speaker: {resref: mood}}
OUTPUT_DIR = Path("voices_moods_pst")
MANIFEST_PATH = OUTPUT_DIR / "moods.json"

# General mood vocabulary; descriptions go into moods.json for the line classifier
MOODS: Dict[str, str] = {
    "neutral": "plain, conversational, informative",
    "warm": "friendly, affectionate, gentle, tender",
    "playful": "amused, teasing, joking, laughing",
    "sarcastic": "dry, mocking, scornful, contemptuous",
    "angry": "hostile, shouting, threatening, furious",
    "sad": "sorrowful, regretful, wistful, pained",
    "fearful": "nervous, anxious, startled, pleading",
    "solemn": "grave, formal, philosophical, weighty",
}
SKIP = "skip"                                # label for unusable clips

TARGET_SECONDS = 8.0       # stop adding clips once a reference reaches this
MAX_SECONDS = 10.0         # keep refs short; Drama 3 stacks several up to ~30 s
MIN_MOOD_SECONDS = 5.0     # a mood needs at least this much audio to exist
MIN_CLIP_SECONDS = 2.0
CLIP_GAP = 0.25            # silence between joined clips
# =======================================================


# ==================== COLLECT ====================

def spoken_part(text: str, speaker: str) -> str:
    """The words the character actually says in a line (narration removed)."""
    parts = [seg for voice, seg in split_segments(text, speaker) if voice != NARRATOR]
    return " ".join(parts).replace("*", "").strip()


def audio_stats(path: Path) -> Tuple[float, float, float]:
    """Return (duration seconds, mean dB, max dB) via ffmpeg."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    duration = mean_db = max_db = 0.0
    for line in result.stderr.splitlines():
        if "Duration:" in line:
            h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
            duration = int(h) * 3600 + int(m) * 60 + float(s)
        elif "mean_volume:" in line:
            mean_db = float(line.split("mean_volume:")[1].split()[0])
        elif "max_volume:" in line:
            max_db = float(line.split("max_volume:")[1].split()[0])
    return duration, mean_db, max_db


def voiced_clips(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Every original voiced dialog line, one per sound resref.

    Args:
        rows: dialog-report CSV rows.

    Returns:
        Dicts with Speaker, StrRef, SoundResRef, Spoken and Text.
    """
    clips: Dict[str, Dict[str, str]] = {}
    for r in rows:
        if not r["RealName"] or not is_original_sound(r["SoundResRef"]):
            continue
        resref = r["SoundResRef"].upper()
        spoken = spoken_part(r["Text"], r["RealName"])
        if resref in clips or not spoken:
            continue
        clips[resref] = {"Speaker": r["RealName"], "StrRef": r["StrRef"], "SoundResRef": resref,
                         "Spoken": spoken, "Text": r["Text"]}
    return sorted(clips.values(), key=lambda c: (c["Speaker"], int(c["StrRef"])))


def extract_clips(resrefs: List[str]) -> None:
    """Extract sound resrefs from the game (override first, then BIFFs) and decode WAVC."""
    from pst_revoice import extract_original_sounds
    extract_original_sounds(resrefs, CLIPS_DIR)


def collect() -> None:
    """Extract every voiced dialog clip and write the clip table."""
    with open(cfg.CSV_PATH, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    clips = voiced_clips(rows)
    extract_clips([c["SoundResRef"] for c in clips])

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    kept = 0
    with CLIPS_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Speaker", "StrRef", "SoundResRef", "Duration", "MeanDb", "MaxDb",
                         "WordsPerSec", "Spoken", "Text"])
        for c in clips:
            path = CLIPS_DIR / f"{c['SoundResRef']}.WAV"
            if not path.exists():
                logger.warning(f"⚠️ Missing audio for {c['Speaker']} {c['SoundResRef']}")
                continue
            duration, mean_db, max_db = audio_stats(path)
            wps = len(c["Spoken"].split()) / duration if duration else 0.0
            writer.writerow([c["Speaker"], c["StrRef"], c["SoundResRef"], f"{duration:.2f}",
                             f"{mean_db:.1f}", f"{max_db:.1f}", f"{wps:.2f}", c["Spoken"], c["Text"]])
            kept += 1
    logger.info(f"📋 {CLIPS_CSV}: {kept} clips for {len({c['Speaker'] for c in clips})} characters")


# ==================== BUILD ====================

def read_clips() -> List[Dict[str, str]]:
    """Read the clip table."""
    with CLIPS_CSV.open("r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def read_labels() -> Dict[str, Dict[str, str]]:
    """Read pst-mood-labels.json ({speaker: {resref: mood}})."""
    if not LABELS_PATH.exists():
        return {}
    return json.loads(LABELS_PATH.read_text(encoding="utf-8"))


def pick_clips(candidates: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    """
    Choose clips for one reference: longest first, up to MAX_SECONDS, stopping at TARGET_SECONDS.

    Args:
        candidates: (resref, duration) pairs sharing a mood.

    Returns:
        The chosen pairs, or [] if they don't reach MIN_MOOD_SECONDS.
    """
    picked: List[Tuple[str, float]] = []
    total = 0.0
    for resref, duration in sorted(candidates, key=lambda c: -c[1]):
        if duration < MIN_CLIP_SECONDS or total + duration + CLIP_GAP * len(picked) > MAX_SECONDS:
            continue
        picked.append((resref, duration))
        total += duration
        if total >= TARGET_SECONDS:
            break
    return picked if total >= MIN_MOOD_SECONDS else []


def join_clips(resrefs: List[str], out_path: Path) -> bool:
    """Concatenate clips with CLIP_GAP silence into one mono WAV at the original game rate."""
    rate = SAMPLE_RATE
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for r in resrefs:
        cmd += ["-i", str(CLIPS_DIR / f"{r}.WAV")]
    cmd += ["-f", "lavfi", "-t", str(CLIP_GAP), "-i", f"anullsrc=r={rate}:cl=mono"]
    gap = len(resrefs)
    n_gaps = len(resrefs) - 1
    filters = [f"[{i}:a]aresample={rate},aformat=channel_layouts=mono[s{i}]" for i in range(len(resrefs))]
    if n_gaps:
        filters.append(f"[{gap}:a]asplit={n_gaps}" + "".join(f"[g{i}]" for i in range(n_gaps)))
    chain = "".join(f"[s{i}]" + (f"[g{i}]" if i < n_gaps else "") for i in range(len(resrefs)))
    filters.append(f"{chain}concat=n={len(resrefs) + n_gaps}:v=0:a=1[out]")
    cmd += ["-filter_complex", ";".join(filters), "-map", "[out]",
            "-ar", str(rate), "-ac", "1", "-c:a", "pcm_s16le", str(out_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"❌ ffmpeg failed for {out_path}: {result.stderr.strip()}")
        return False
    return True


def group_by_mood(clips: List[Dict[str, str]], labels: Dict[str, Dict[str, str]]
                  ) -> Dict[str, Dict[str, List[Tuple[str, float]]]]:
    """
    Group labelled clips: {speaker: {mood: [(resref, duration)]}}. Unlabelled clips count as neutral.

    Raises:
        ValueError: If a label isn't in MOODS (or "skip").
    """
    groups: Dict[str, Dict[str, List[Tuple[str, float]]]] = defaultdict(lambda: defaultdict(list))
    for c in clips:
        mood = labels.get(c["Speaker"], {}).get(c["SoundResRef"], "neutral")
        if mood == SKIP:
            continue
        if mood not in MOODS:
            raise ValueError(f"Unknown mood '{mood}' for {c['Speaker']} {c['SoundResRef']}")
        groups[c["Speaker"]][mood].append((c["SoundResRef"], float(c["Duration"])))
    return groups


def build_manifest(groups: Dict[str, Dict[str, List[Tuple[str, float]]]],
                   spoken: Dict[str, str], defaults: Optional[Dict[str, str]] = None) -> Dict[str, Dict]:
    """
    Decide each character's references (without writing audio).

    Args:
        groups: Output of group_by_mood().
        spoken: resref -> spoken transcript.
        defaults: speaker -> preferred default mood ("_default" in the labels file).

    Returns:
        {speaker: {"default": mood, "moods": {mood: {...}}}}
    """
    manifest: Dict[str, Dict] = {}
    for speaker, moods in sorted(groups.items()):
        built: Dict[str, Dict] = {}
        for mood, candidates in moods.items():
            picked = pick_clips(candidates)
            if not picked:
                continue
            built[mood] = {
                "description": MOODS[mood],
                "clips": [r for r, _ in picked],
                "seconds": round(sum(d for _, d in picked), 2),
                "text": " ".join(spoken[r] for r, _ in picked),
            }
        if not built:
            # Nothing reaches MIN_MOOD_SECONDS on its own - pool every clip into one reference
            pooled = pick_clips([c for cands in moods.values() for c in cands])
            if pooled:
                built["neutral"] = {"description": MOODS["neutral"], "clips": [r for r, _ in pooled],
                                    "seconds": round(sum(d for _, d in pooled), 2),
                                    "text": " ".join(spoken[r] for r, _ in pooled)}
        if not built:
            logger.warning(f"⚠️ {speaker}: not enough audio for any reference")
            continue
        default = (defaults or {}).get(speaker)
        if default not in built:
            default = "neutral" if "neutral" in built else max(built, key=lambda m: built[m]["seconds"])
        manifest[speaker] = {"default": default, "moods": dict(sorted(built.items()))}
    return manifest


def build(speakers: Optional[List[str]] = None) -> None:
    """Write mood reference WAV/TXT files and moods.json (optionally only some characters)."""
    clips = read_clips()
    if speakers:
        clips = [c for c in clips if c["Speaker"] in speakers]
    labels = read_labels()
    manifest = build_manifest(group_by_mood(clips, labels), {c["SoundResRef"]: c["Spoken"] for c in clips},
                              {s: m["_default"] for s, m in labels.items() if "_default" in m})

    for speaker, entry in manifest.items():
        out_dir = OUTPUT_DIR / speaker
        out_dir.mkdir(parents=True, exist_ok=True)
        for mood, info in entry["moods"].items():
            wav = out_dir / f"{mood}.wav"
            if join_clips(info["clips"], wav):
                (out_dir / f"{mood}.txt").write_text(info["text"], encoding="utf-8")
                info["wav"] = wav.as_posix()
        logger.info(f"🎭 {speaker}: default={entry['default']}, "
                    + ", ".join(f"{m} {i['seconds']:.1f}s" for m, i in entry["moods"].items()))

    # Merge into an existing manifest so building one character keeps the others
    existing = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.exists() else {}
    characters = {**existing.get("characters", {}), **manifest}
    MANIFEST_PATH.write_text(json.dumps({"vocabulary": MOODS, "characters": characters}, indent=2,
                                        ensure_ascii=False), encoding="utf-8")
    logger.info(f"📦 {MANIFEST_PATH}: {len(manifest)} characters")


def status() -> None:
    """Print labelled seconds per character and mood."""
    clips = read_clips()
    labels = read_labels()
    for speaker in sorted({c["Speaker"] for c in clips}):
        mine = [c for c in clips if c["Speaker"] == speaker]
        unlabelled = sum(1 for c in mine if c["SoundResRef"] not in labels.get(speaker, {}))
        seconds: Dict[str, float] = defaultdict(float)
        for c in mine:
            seconds[labels.get(speaker, {}).get(c["SoundResRef"], "unlabelled")] += float(c["Duration"])
        summary = ", ".join(f"{m} {s:.1f}s" for m, s in sorted(seconds.items(), key=lambda x: -x[1]))
        print(f"{speaker}: {len(mine)} clips, {unlabelled} unlabelled | {summary}")


# ==================== MAIN ====================

def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["collect", "status", "build"])
    parser.add_argument("--speaker", action="append", help="build: only this character (repeatable)")
    args = parser.parse_args(argv)
    if args.command == "build":
        build(args.speaker)
    else:
        {"collect": collect, "status": status}[args.command]()


if __name__ == "__main__":
    main()
