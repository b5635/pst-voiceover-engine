"""
Voice references for the PST Drama 3 revoice.

Picks the reference clip (wav + transcript) a segment is cloned from: the
narrator's clip, a character's mood clip (voices_moods_pst/, picked per line in
pst_revoice/moods.csv) or a cast NPC's voice library clip.
"""

import csv
import json
from pathlib import Path
from typing import Dict, Optional, Tuple

# ==================== CONFIGURATION ====================
MOODS_DIR = Path("voices_moods_pst")
MANIFEST_PATH = MOODS_DIR / "moods.json"
MOODS_CSV = Path("pst_revoice/moods.csv")
NARRATOR_REF = MOODS_DIR / "Narrator" / "neutral"          # .wav + .txt; the folder name is the narrator voice
LIBRARY_CATALOGS = [Path("voice_library/nwn/catalog.json"), Path("voice_library/wow/catalog.json")]
# =======================================================


def load_moods() -> Tuple[Dict, Dict[int, Dict[str, str]]]:
    """Read moods.json (characters) and the classifier's moods.csv (picks by strref)."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["characters"]
    picks: Dict[int, Dict[str, str]] = {}
    if MOODS_CSV.exists():
        with MOODS_CSV.open("r", encoding="utf-8-sig") as f:
            picks = {int(r["StrRef"]): r for r in csv.DictReader(f)}
    return manifest, picks


def load_library() -> Dict[str, Dict]:
    """Voice library entries by id (voicelib_nwn.py / voicelib_wow.py catalogs)."""
    voices: Dict[str, Dict] = {}
    for path in LIBRARY_CATALOGS:
        if path.exists():
            voices.update({v["id"]: v for v in json.loads(path.read_text(encoding="utf-8"))})
    return voices


def reference_for(voice: str, strref: int, manifest: Dict, picks: Dict[int, Dict[str, str]],
                  library: Optional[Dict[str, Dict]] = None) -> Optional[Tuple[Path, Optional[str], str]]:
    """
    (wav, transcript, mood) for one segment's voice, or None if there is no reference.

    The narrator always uses NARRATOR_REF; a character uses the mood picked for
    this line, falling back to their default mood; a cast NPC uses its library
    voice's reference clip (transcript None when the clip was never transcribed).
    """
    library = library or {}
    if voice == NARRATOR_REF.parent.name:
        return NARRATOR_REF.with_suffix(".wav"), NARRATOR_REF.with_suffix(".txt").read_text(encoding="utf-8"), "-"
    if voice in library:
        v = library[voice]
        return Path(v["reference_wav"]), v.get("reference_text") or None, voice
    entry = manifest.get(voice)
    if not entry:
        return None
    mood = picks.get(strref, {}).get("Mood", entry["default"])
    if mood not in entry["moods"]:
        mood = entry["default"]
    info = entry["moods"][mood]
    return Path(info["wav"]), info["text"], mood
