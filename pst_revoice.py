#!/usr/bin/env python3
"""
PST Revoice
Adds voice to Planescape: Torment without replacing original performances.

    - if the game already voiced a line, that WAV is kept (extracted into
      pst_revoice/original/). Narration around the quote is generated and
      stitched onto it
    - originally silent lines are generated: cloned main-cast voices from
      voices_moods_pst/ (or a borrowed voice from pst-voice-aliases.json),
      everyone else from the library voice pst_cast.py assigned
    - unquoted narration is spoken by the narrator

Every line still gets a new sound resref (VV<strref>), so the original voice
files in the game folder are never overwritten.

Day-to-day work is run_pst.bat (pst_gui.py): Strata directs silent lines, Fish
Audio Drama 3 generates new speech (and narrator on original VO), then this
script stages the mod from audio_fish/.

    python pst_revoice.py plan       # list every line that gets a new voice
    python pst_revoice.py build      # stage the WeiDU mod from audio_fish
"""

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from libs.appconfig import cfg
from libs.utils import convert_replacement, decode_wavc, preprocess_text, setup_logging

# Ensure UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

logger = setup_logging(Path(__file__).stem)

# ==================== CONFIGURATION ====================
WORK_DIR = Path("pst_revoice")
PLAN_PATH = WORK_DIR / "plan.csv"
PLAN_META_PATH = WORK_DIR / "plan_meta.json"  # dialog/casting fingerprints when the plan was written
AUDIO_DIR = WORK_DIR / "audio"               # VoiceBox (deprecated)
FISH_AUDIO_DIR = WORK_DIR / "audio_fish"     # Drama 3 WAVs; what build copies
ORIGINAL_DIR = WORK_DIR / "original"         # decoded original game WAVs (SoundResRef)
MOOD_CLIPS_DIR = Path("pst_moods/clips")     # already-extracted clips from Collect clone clips
MOD_DIR = Path("mod-pst-revoice")
WEIDU_BATCH = 80                             # --biff-get flags per WeiDU call (Windows argv limit)
MOD_NAME = "PST_Revoiced"

PRONUNCIATION_PATH = Path("pst-pronunciation.json")    # per-character TTS spelling fixes
LINE_OVERRIDES_PATH = Path("pst-line-overrides.json")  # hand-edited TTS text per strref
LINE_NOTES_PATH = Path("pst-line-notes.json")          # per-strref notes (GUI / later review)
GENERATION_PATH = WORK_DIR / "generation.json"         # speaker + pronunciation used per generated line
VOICE_ALIASES_PATH = Path("pst-voice-aliases.json")    # unvoiced speakers -> voice they borrow
CASTING_PATH = Path("pst_cast/casting.json")           # pst_cast.py: DLG -> library voice
CAST_NPCS_PATH = Path("pst_cast/npcs.json")            # pst_npc_profiles.py: DLG -> lines
CAST_OVERRIDES_PATH = Path("pst-cast-overrides.json")  # hand-picked speaker/voice; beats casting.json

NARRATOR = "Narrator"                        # VoiceBox profile used for narration
# Dialog.tlk RealNames that shipped with original conversation VO. Everyone else
# was silent in the OG game and gets a library voice (Cast NPCs), not a clone.
ORIGINAL_VOICED = (
    "Annah",
    "Dak'kon",
    "Deionarra",
    "Fall-from-Grace",
    "Fhjull Forked-Tongue",
    "Ignus",
    "Morte",
    "Nordom",
    "Ravel",
    "The Transcendent One",
    "Trias",
    "Vhailor",
)
NEW_RESREF_PREFIX = "VV"                     # every new line: VV<strref>
SEGMENT_GAP = 0.3                            # seconds of silence between segments
TARGET_LUFS = -21.0                          # loudness of every segment and the stitched line
LOUDNORM_TP = -1.5                           # true-peak ceiling (dBTP)
LOUDNORM_LRA = 11.0                          # allowed loudness range
SAMPLE_RATE = 22050                          # classic PST: WAVC/ACM decode is 22.05 kHz mono 16-bit
                                             # Drama 3 synthesizes at 44.1 kHz; stitch downsamples to this

PLACEHOLDER_RE = re.compile(r"^\s*(\[.*\]|null[ _]node)?\s*$", re.IGNORECASE)
# =======================================================


@dataclass
class PlanRow:
    """
    One strref that gets a new voice.

    Attributes:
        strref: The string reference.
        speaker: RealName of the speaking character.
        action: Always "generate" (kept as a column for older plan files).
        resref: Sound resref the strref will point to (VV<strref>).
        text: Dialog.tlk string. Dialog rows are STRING_SET on build; examine
            (info-point) rows are not — GemRB only patches the sound resref.
        voice: Voice for the speech, when it isn't voice_for(speaker): a voice
            library id ("nwn:...", "wow:...") cast by pst_cast.py.
        original: Original game SoundResRef when this line already had VO
            (MRT001, ANA001, ...). Empty for originally silent lines.
    """
    strref: int
    speaker: str
    action: str
    resref: str
    text: str
    voice: str = ""
    original: str = ""

    @property
    def speech_voice(self) -> str:
        """The voice this line's speech uses."""
        return self.voice or voice_for(self.speaker)

    @property
    def is_infopoint(self) -> bool:
        """True for ARE region type 1 examine lines (Narrator, not dialog)."""
        return self.speaker == NARRATOR

    @property
    def keeps_original(self) -> bool:
        """True when this line should use the original game WAV for speech."""
        return bool(self.original)


# ==================== SEGMENTING ====================

def _clean(text: str) -> str:
    """Collapse whitespace (asterisks are kept for the patcher-config rules)."""
    return re.sub(r"\s+", " ", text).strip()


def prepare_segment_text(text: str, patcher_config: Dict) -> str:
    """
    Turn a segment into TTS input.

    Applies the patcher-config tokens and phonetic rules (which silence
    *sigh*-style effects and handle *emphasis*), then drops leftover asterisks.

    Args:
        text: Segment text from split_segments().
        patcher_config: Loaded patcher config.

    Returns:
        Text to send to VoiceBox; may be empty if nothing speakable remains.
    """
    text = _clean(preprocess_text(text, patcher_config).replace("*", ""))
    # "brothers/sisters", "and/or": TTS would say "slash"
    return re.sub(r"(?<=[A-Za-z])/(?=[A-Za-z])", ", ", text)


def load_pronunciation(path: Path = PRONUNCIATION_PATH) -> Dict[str, List[Dict[str, str]]]:
    """Read per-character pronunciation rules ({speaker: [{"pattern", "replacement"}]})."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load_line_overrides(path: Path = LINE_OVERRIDES_PATH) -> Dict[int, str]:
    """Read hand-edited TTS text per strref (the on-screen text is unchanged)."""
    if not path.exists():
        return {}
    return {int(k): v for k, v in json.loads(path.read_text(encoding="utf-8")).items()}


@lru_cache(maxsize=None)
def load_voice_aliases(path: Path = VOICE_ALIASES_PATH) -> Dict[str, str]:
    """Read {speaker: voice} for speakers who borrow another character's voice."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def voice_for(speaker: str) -> str:
    """The voice (profile / mood reference name) a speaker's speech uses."""
    return load_voice_aliases().get(speaker, speaker)


def load_cast_voices(casting_path: Path = CASTING_PATH,
                      npcs_path: Path = CAST_NPCS_PATH) -> Dict[int, Tuple[str, str]]:
    """
    strref -> (library voice id, speaker) for every line of every dialog pst_cast.py cast,
    including lines that had original audio (they are replaced too).
    """
    if not casting_path.exists() or not npcs_path.exists():
        return {}
    casting = json.loads(casting_path.read_text(encoding="utf-8"))
    result: Dict[int, Tuple[str, str]] = {}
    for npc in json.loads(npcs_path.read_text(encoding="utf-8")):
        cast = casting.get(npc["dialog"], {})
        if not cast.get("voice_id") or npc.get("already_voiced"):
            continue
        for ln in npc["lines"]:
            result.setdefault(ln["strref"], (cast["voice_id"], npc["speaker"] or npc["dialog"]))
    return result


def dialog_of_strrefs(npcs_path: Path = CAST_NPCS_PATH) -> Dict[int, str]:
    """strref -> DLG resref, from pst_cast/npcs.json (empty if missing)."""
    if not npcs_path.exists():
        return {}
    result: Dict[int, str] = {}
    for npc in json.loads(npcs_path.read_text(encoding="utf-8")):
        for ln in npc.get("lines") or []:
            result.setdefault(int(ln["strref"]), npc["dialog"])
    return result


def load_cast_overrides(path: Path = CAST_OVERRIDES_PATH) -> Dict[str, Dict]:
    """
    Hand-picked speaker / voice choices that beat pst_cast/casting.json and the
    dialog report until removed.

    Shape: {"dialogs": {DLG: {"speaker"?, "voice"?, "set_at"}},
            "lines": {strref: {"speaker"?, "voice"?, "set_at"}}}.
    A line entry beats its dialog's entry field by field.
    """
    empty = {"dialogs": {}, "lines": {}}
    if not path.exists():
        return empty
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "dialogs": {str(k).upper(): v for k, v in (data.get("dialogs") or {}).items()
                    if not str(k).startswith("_") and isinstance(v, dict)},
        "lines": {str(k): v for k, v in (data.get("lines") or {}).items()
                  if not str(k).startswith("_") and isinstance(v, dict)},
    }


def save_cast_overrides(overrides: Dict[str, Dict], path: Path = CAST_OVERRIDES_PATH) -> None:
    """Write pst-cast-overrides.json (entries with neither speaker nor voice are dropped)."""
    def clean(entries: Dict) -> Dict:
        kept = {}
        for key, entry in sorted(entries.items(), key=lambda kv: (len(kv[0]), kv[0])):
            entry = {k: v for k, v in entry.items() if v}
            if entry.get("speaker") or entry.get("voice"):
                kept[key] = entry
        return kept
    payload = {
        "_comment": "Hand-picked speaker/voice (PST Revoice GUI). Beats pst_cast/casting.json and the "
                    "dialog report until removed. Line entries beat dialog entries.",
        "dialogs": clean(overrides.get("dialogs") or {}),
        "lines": clean(overrides.get("lines") or {}),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def cast_override_for(strref: int, overrides: Dict[str, Dict], dialog: str = "") -> Dict[str, str]:
    """Merged {"speaker"?, "voice"?} for one line (line beats dialog), or {}."""
    merged: Dict[str, str] = {}
    for entry in ((overrides.get("dialogs") or {}).get((dialog or "").upper()) or {},
                  (overrides.get("lines") or {}).get(str(strref)) or {}):
        for key in ("speaker", "voice"):
            if entry.get(key):
                merged[key] = entry[key]
    return merged


def apply_cast_override(row: "PlanRow", override: Dict[str, str]) -> None:
    """
    Put a hand-picked speaker / voice on a plan row in place.

    A new speaker without a voice pick uses that speaker's own voice (clone or
    alias), dropping any library voice the old speaker was cast with.
    """
    if not override:
        return
    if override.get("speaker") and override["speaker"] != row.speaker:
        row.speaker = override["speaker"]
        row.voice = ""
    if override.get("voice"):
        row.voice = "" if override["voice"] == voice_for(row.speaker) else override["voice"]


def apply_cast_overrides(plan: List["PlanRow"], overrides: Optional[Dict[str, Dict]] = None,
                         dialog_of: Optional[Dict[int, str]] = None) -> int:
    """Apply pst-cast-overrides.json to every row; returns how many rows matched one."""
    overrides = overrides if overrides is not None else load_cast_overrides()
    if not (overrides.get("dialogs") or overrides.get("lines")):
        return 0
    dialog_of = dialog_of if dialog_of is not None else dialog_of_strrefs()
    hits = 0
    for row in plan:
        override = cast_override_for(row.strref, overrides, dialog_of.get(row.strref, ""))
        if override:
            apply_cast_override(row, override)
            hits += 1
    return hits


@lru_cache(maxsize=None)
def profile_voices() -> Set[str]:
    """Characters that have their own cloned voice (voices_pst/<Name> N.txt)."""
    return {p.stem.rsplit(" ", 1)[0] for p in Path(cfg.VOICES_DIR).glob("*.txt")}


def save_line_overrides(overrides: Dict[int, str], path: Path = LINE_OVERRIDES_PATH) -> None:
    """Write hand-edited TTS text per strref."""
    path.write_text(json.dumps({str(k): v for k, v in sorted(overrides.items())}, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def load_line_notes(path: Path = LINE_NOTES_PATH) -> Dict[int, str]:
    """Read per-strref notes from the lines GUI."""
    if not path.exists():
        return {}
    return {int(k): v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if not str(k).startswith("_")}


def save_line_notes(notes: Dict[int, str], path: Path = LINE_NOTES_PATH) -> None:
    """Write per-strref notes (empty notes are dropped)."""
    payload = {str(k): v for k, v in sorted(notes.items()) if v.strip()}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_generation(path: Path = GENERATION_PATH) -> Dict[int, Dict]:
    """Read speaker / pronunciation snapshots recorded when a line's audio was generated."""
    if not path.exists():
        return {}
    return {int(k): v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if not str(k).startswith("_")}


def save_generation(records: Dict[int, Dict], path: Path = GENERATION_PATH) -> None:
    """Write generation snapshots keyed by strref."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({str(k): v for k, v in sorted(records.items())}, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def _speaker_rules(speaker: str, pronunciation: Dict[str, List[Dict[str, str]]],
                   voice: str = "") -> List[Dict[str, str]]:
    """Pronunciation rules for this speaker (falls back to the speech voice's rules)."""
    return pronunciation.get(speaker) or pronunciation.get(voice or voice_for(speaker), [])


def rules_fingerprint(speaker: str, pronunciation: Dict[str, List[Dict[str, str]]],
                      voice: str = "") -> str:
    """Stable hash of a speaker's full pronunciation rule set (pattern + replacement)."""
    payload = json.dumps([[r.get("pattern", ""), r.get("replacement", "")]
                          for r in _speaker_rules(speaker, pronunciation, voice)],
                         ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def matching_pronunciation_rules(text: str, speaker: str,
                                 pronunciation: Dict[str, List[Dict[str, str]]],
                                 voice: str = "") -> List[Dict[str, str]]:
    """
    Pronunciation rules that actually change this line's speech (never narration).

    Rules are applied in order, same as tts_segments(), so a later rule that only
    matches after an earlier substitution still counts.
    """
    rules = _speaker_rules(speaker, pronunciation, voice)
    if not rules:
        return []
    speech_voice = voice or voice_for(speaker)
    speech = [seg for v, seg in split_segments(text, speech_voice) if v != NARRATOR]
    if not speech:
        return []
    matched: List[Dict[str, str]] = []
    current = speech
    for rule in rules:
        updated = [apply_pronunciation(s, [rule]) for s in current]
        if updated != current:
            matched.append(rule)
        current = updated
    return matched


def generation_snapshot(row: PlanRow, spoken: str, patcher_config: Dict,
                        pronunciation: Dict[str, List[Dict[str, str]]]) -> Dict:
    """
    Record of what the engine spoke and which pronunciation rules applied.

    Stored when audio is generated so later rule/speaker changes can mark the
    line stale only if they would actually change the spoken audio.
    """
    segs = tts_segments(spoken, row.speaker, patcher_config, pronunciation, row.voice)
    rules = matching_pronunciation_rules(spoken, row.speaker, pronunciation, row.voice)
    compact = []
    for r in rules:
        item = {"pattern": r.get("pattern", ""), "replacement": r.get("replacement", "")}
        if r.get("comment"):
            item["comment"] = r["comment"]
        compact.append(item)
    return {
        "speaker": row.speaker,
        "voice": row.speech_voice,
        "spoken": spoken,
        "tts": [list(s) for s in segs],
        "rules": compact,
        "rules_fingerprint": rules_fingerprint(row.speaker, pronunciation, row.voice),
    }


def generation_is_stale(row: PlanRow, spoken: str, patcher_config: Dict,
                        pronunciation: Dict[str, List[Dict[str, str]]],
                        record: Optional[Dict]) -> bool:
    """
    True if existing audio may not match the current speaker / pronunciation.

    No record (generated before tracking): stale only when a pronunciation rule
    currently changes the spoken line. Lines the rules do not touch are left
    alone. With a record, a speaker/voice change, a spoken-text change, or a
    rule change that actually alters TTS text all count as stale.
    """
    if record is None:
        return bool(matching_pronunciation_rules(spoken, row.speaker, pronunciation, row.voice))
    if record.get("speaker") != row.speaker or record.get("voice") != row.speech_voice:
        return True
    if record.get("spoken") != spoken:
        return True
    if record.get("rules_fingerprint") == rules_fingerprint(row.speaker, pronunciation, row.voice):
        return False
    current = generation_snapshot(row, spoken, patcher_config, pronunciation)
    return record.get("tts") != current["tts"]


def apply_pronunciation(text: str, rules: List[Dict[str, str]]) -> str:
    """
    Apply one character's pronunciation rules (regex, case-insensitive unless caseSensitive).

    Example:
        >>> apply_pronunciation("jest an eejit", [{"pattern": r"\\bee+ji?t\\b", "replacement": "idiot"}])
        'jest an idiot'
    """
    for rule in rules:
        flags = 0 if rule.get("caseSensitive") else re.IGNORECASE
        template = convert_replacement(rule.get("replacement", ""))

        def substitute(m: "re.Match[str]", template: str = template) -> str:
            out = m.expand(template)
            # Keep a capitalised match capitalised ("Git" -> "Get", not "get")
            return out[:1].upper() + out[1:] if out and m.group(0)[:1].isupper() else out

        text = re.sub(rule["pattern"], substitute, text, flags=flags)
    return text


def tts_segments(text: str, speaker: str, patcher_config: Dict,
                 pronunciation: Dict[str, List[Dict[str, str]]], voice: str = "") -> List[Tuple[str, str]]:
    """
    The (voice, TTS text) pairs actually sent to the engine for one line.

    The speaker's pronunciation rules apply to their speech only, never to narration.
    Speech uses voice_for(speaker), so aliased speakers get the borrowed voice.

    Args:
        text: Line text (a hand-edited override, if any, or the game text).
        speaker: Speaking character.
        patcher_config: Loaded patcher config (global tokens and phonetic rules).
        pronunciation: Output of load_pronunciation().
        voice: Voice for the speech (PlanRow.voice); default voice_for(speaker).

    Returns:
        Speakable segments in order.
    """
    result = []
    speech_voice = voice or voice_for(speaker)
    rules = _speaker_rules(speaker, pronunciation, voice)
    for voice, seg in convert_emotes(split_segments(text, speech_voice), speech_voice):
        if voice != NARRATOR:
            seg = apply_pronunciation(seg, rules)
        tts_text = prepare_segment_text(seg, patcher_config)
        if not any(c.isalnum() for c in tts_text):
            continue
        if result and result[-1][0] == voice:
            # A dropped segment in between (e.g. "Hsst!") leaves the same voice twice in a row
            result[-1] = (voice, f"{result[-1][1]} {tts_text}")
        else:
            result.append((voice, tts_text))
    return result


# ::Emote:: barks (::Growl::, ::Sigh::) -> a sound the voice makes. Emotes with no sound
# (::Looks Confused::, ::Sip::) are dropped rather than read out as words.
EMOTE_RE = re.compile(r"^::\s*(.+?)\s*::$")
# A segment made only of short *emotes* ("*Cough* - *Cough*", "*Hic*", "*Sigh*...")
STAR_EMOTE_RE = re.compile(r"\*\s*([A-Za-z][A-Za-z !'-]{0,24}?)\s*!?\*")
STAR_EMOTE_TOKEN_RE = re.compile(r"\*[^*]{1,26}\*")
STAR_EMOTE_GLUE = set(" \t\n.,!?-—…")


class _StarEmoteOnly:
    """A segment made only of short *emote* marks and punctuation (linear time, no regex backtracking)."""

    @staticmethod
    def match(seg: str) -> bool:
        if len(seg) > 200 or not STAR_EMOTE_TOKEN_RE.search(seg):
            return False
        return all(c in STAR_EMOTE_GLUE for c in STAR_EMOTE_TOKEN_RE.sub("", seg))


STAR_EMOTE_ONLY_RE = _StarEmoteOnly()
EMOTE_SOUNDS = (
    ("hiccup", "Hic!"), ("hic", "Hic!"), ("glug", "Glug, glug, glug."), ("snort", "Hmph!"),
    ("cough", "Ahem! Hem."), ("hiss", "Hsssss!"),
    ("growl", "Grrrrrr..."), ("drunken laugh", "Ha ha ha... hic... ha ha!"), ("laugh", "Ha ha ha ha!"),
    ("giggle", "Hee hee hee!"), ("snicker", "Heh heh heh..."), ("chuckle", "Heh heh."),
    ("sigh", "Haaah..."), ("moan", "Uuuuhhhh..."), ("groan", "Uuuhhh..."), ("ugg", "Uggghhh..."),
    ("grunt", "Hngh! Hngh! Hngh!"), ("mumble", "Mmm... mm-hmm... mmm..."), ("grumble", "Mmph... mmm... hmph."),
    ("mutter", "Mmm... mm..."), ("whistle", "Hm hm hmm, hm hmm hmm."), ("hum", "Hm hm hmm, hm hmm hmm."),
    ("yawn", "Aaaahhh... hmm."), ("kiss", "Mwah!"), ("pout", "Hmph!"), ("sulk", "Hmph."), ("mope", "Haaah..."),
    ("cough", "Ahem! Hem."), ("sniff", "Hmf."), ("hiss", "Hsssss!"), ("scream", "Aaaaah!"),
)


def emote_sound(emote: str) -> str:
    """The sound for one ::emote:: text, or "" when it makes none (it is then dropped)."""
    match = EMOTE_RE.match(emote.strip())
    if match:
        names = [match.group(1)]
    elif STAR_EMOTE_ONLY_RE.match(emote.strip()):
        names = STAR_EMOTE_RE.findall(emote)
    else:
        return ""
    sounds = [next((sound for key, sound in EMOTE_SOUNDS if key in n.lower()), "") for n in names]
    return " ".join(s for s in sounds if s)


def is_emote(segment: str) -> bool:
    """A segment that is only ::emote:: or *emote* marks."""
    seg = segment.strip()
    return bool(EMOTE_RE.match(seg) or STAR_EMOTE_ONLY_RE.match(seg))


def convert_emotes(segments: List[Tuple[str, str]], speech_voice: str) -> List[Tuple[str, str]]:
    """
    Replace ::emote:: segments with the sound they stand for, spoken by the
    character (a narrator-read "::Yawn::" before a quote joins that quote);
    soundless emotes are dropped.
    """
    out: List[Tuple[str, str]] = []
    for voice, seg in segments:
        if not is_emote(seg):
            out.append((voice, seg))
            continue
        sound = emote_sound(seg)
        if not sound:
            continue
        if out and out[-1][0] == speech_voice:
            out[-1] = (speech_voice, f"{out[-1][1]} {sound}")
        else:
            out.append((speech_voice, sound))
    merged: List[Tuple[str, str]] = []
    for voice, seg in out:
        if merged and merged[-1][0] == voice:
            merged[-1] = (voice, f"{merged[-1][1]} {seg}")
        else:
            merged.append((voice, seg))
    return merged


def _is_spoken_without_quotes(text: str) -> bool:
    """
    Detect lines that are speech even though they carry no quotes.

    PST writes some voices without quotes: the Transcendent One speaks in
    ALL CAPS, Vhailor's lines are wrapped in *asterisks*, and idle barks use
    ::emotes:: like ::Yawn::.
    """
    stripped = text.strip()
    if len(stripped) > 1 and stripped.startswith("*") and stripped.endswith("*"):
        return True
    if stripped.startswith("::") and stripped.endswith("::") and len(stripped) > 4:
        return True
    letters = [c for c in stripped if c.isalpha()]
    return len(letters) >= 3 and sum(c.isupper() for c in letters) / len(letters) >= 0.9


# A quote right after "...reads:" / "...written:" is text being read out (a sign, a scroll, a tattoo)
READ_OUT_RE = re.compile(
    r"(\b(it|calligraphy|notice|sign|note|scroll|page|inscription|plaque|text|letters|writing|tattoo|message|"
    r"label|entry|lines?|words?|script|runes?|graffiti)\s+(reads?|says)\s*:\s*$)"
    r"|(\b(written|inscribed|engraved|carved|scrawled|scratched|etched|tattooed|painted)\b[^.!?\"]*:\s*$)",
    re.IGNORECASE)
# A quote right after "you mutter," / "you reply:" is the Nameless One's own words: the narrator
# reads them (the player has no voice), never the NPC whose dialog it is.
PLAYER_SAYS_RE = re.compile(
    r"\byou\s+(?:\w+ly\s+)?(say|said|mutter|reply|replied|ask|asked|whisper|answer|respond|call|shout|tell|"
    r"growl|snap|hiss|add)\w*\b[^.!?\"]{0,40}[,:]\s*$", re.IGNORECASE)
# Tutorial hints ("NOTE: You have been assigned a quest...") are UI text, not narration: never voiced
UI_NOTE_RE = re.compile(r"(?:^|\n)[ \t]*NOTE:.*?(?=\n[ \t]*\n|\Z)", re.DOTALL)
STAR_SPAN_RE = re.compile(r"\*([^*]+)\*")
STAR_SPEECH_MIN_WORDS = 3                     # *three or more words* is speech, *one* is emphasis
QUOTED_TERM_MAX_WORDS = 4
SENTENCE_END = tuple(".!?,;:-—…~)")


def fix_doubled_quotes(text: str) -> str:
    """Collapse the game's doubled-quote typos ('""Huh?"') to one quote; an empty "" pair is never speech."""
    return re.sub(r'"{2,}', '"', text)


def is_quoted_term(quote: str, before: str, after: str) -> bool:
    """
    True when a quoted span inside narration is a word being named or read
    (an inscription, a number, a title), not speech:

        The number on his forehead reads "1664".
        The word "REPENT" has been engraved ...
        he is saying the mark on your shoulder is "torment".

    A short quote (<= 4 words) qualifies when narration comes before it and it
    has no sentence punctuation of its own ('"Wait," he says' stays speech),
    or when it is only digits.
    """
    q = quote.strip()
    if not q:
        return False
    if re.fullmatch(r"[\d\s,.\-#]+", q):
        return True
    if not before.strip() or len(q.split()) > QUOTED_TERM_MAX_WORDS or q.endswith(SENTENCE_END):
        return False
    nxt = after.lstrip()[:1]
    return not nxt or not nxt.isupper() or after[:1] in ".,;:!?)"


def split_segments(text: str, speaker: str) -> List[Tuple[str, str]]:
    """
    Split a PST dialog line into (voice, text) segments.

    Text inside double quotes is spoken by the character; everything outside
    is narration. An unclosed quote runs to the end of the line. Lines with
    no quotes are narration unless _is_spoken_without_quotes() says otherwise.
    Doubled quotes ('""Huh?"') count as one. Quotes that are read out
    ('The calligraphy reads: "..."') or name a word / number inside narration
    (is_quoted_term) stay with the narrator, quote marks included.

    Args:
        text: The dialog line.
        speaker: Voice profile name for quoted speech.

    Returns:
        Ordered list of (profile_name, segment_text); empty for placeholders.

    Example:
        >>> split_segments('He pauses. "Then I\\'ll tell you."', "Morte")
        [('Narrator', 'He pauses.'), ('Morte', "Then I'll tell you.")]
        >>> split_segments('The number reads "1664". He waits.', "Zombie")
        [('Narrator', 'The number reads "1664". He waits.')]
    """
    if PLACEHOLDER_RE.match(text):
        return []

    text = fix_doubled_quotes(UI_NOTE_RE.sub("", text))
    if not text.strip():
        return []
    if '"' not in text and not _is_spoken_without_quotes(text):
        # Vhailor & co. mark speech with *asterisks*: a multi-word *span* in a quote-less line is speech
        text = STAR_SPAN_RE.sub(
            lambda m: f'"{m.group(1).strip()}"' if len(m.group(1).split()) >= STAR_SPEECH_MIN_WORDS
            else m.group(0), text)
    if '"' not in text:
        voice = speaker if _is_spoken_without_quotes(text) else NARRATOR
        cleaned = _clean(text)
        return [(voice, cleaned)] if cleaned else []

    parts = text.split('"')
    # (voice, raw text); a quote kept by the narrator is glued to its narration with its quote marks
    pieces: List[List[str]] = []
    reading = False
    for idx, part in enumerate(parts):
        if idx % 2 == 0:
            if pieces and pieces[-1][0] == NARRATOR and pieces[-1][2] == "glue":
                pieces[-1][1] += part
                pieces[-1][2] = ""
            else:
                pieces.append([NARRATOR, part, ""])
            if part.strip():
                reading = bool(READ_OUT_RE.search(part) or PLAYER_SAYS_RE.search(part))
            continue
        before = parts[idx - 1]
        after = parts[idx + 1] if idx + 1 < len(parts) else ""
        closed = idx + 1 < len(parts)
        narrated = closed and (reading or is_quoted_term(part, before, after))
        if narrated and pieces and pieces[-1][0] == NARRATOR:
            pieces[-1][1] += f'"{part}"'
            pieces[-1][2] = "glue"
        elif narrated:
            pieces.append([NARRATOR, f'"{part}"', "glue"])
        else:
            pieces.append([speaker, part, ""])
            reading = False

    segments: List[Tuple[str, str]] = []
    for voice, raw, _ in pieces:
        cleaned = _clean(raw)
        if not any(c.isalnum() for c in cleaned.replace("*", "")):
            continue
        if segments and segments[-1][0] == voice:
            segments[-1] = (voice, f"{segments[-1][1]} {cleaned}")
        else:
            segments.append((voice, cleaned))
    return segments


# ==================== PLAN ====================

def is_original_sound(sound_resref: str) -> bool:
    """True if the resref is original game audio (not empty, not generated)."""
    return bool(sound_resref) and not sound_resref.upper().startswith(cfg.FILENAME_PREFIX.upper())


def needs_direction(row: PlanRow, spoken: str) -> bool:
    """
    True when Drama 3 will speak this line's character speech.

    Originally voiced lines keep the game WAV, so they are not directed.
    Narration-only lines use the narrator's fixed brief.
    """
    if row.original:
        return False
    return any(voice != NARRATOR for voice, _ in split_segments(spoken, row.speaker))


def has_narration(text: str, speaker: str) -> bool:
    """True when split_segments() yields at least one narrator part."""
    return any(voice == NARRATOR for voice, _ in split_segments(text, speaker))


def _wav_named(folder: Path, resref: str) -> Optional[Path]:
    """WAV in folder whose stem matches resref, ignoring case. None if missing."""
    if not resref or not folder.exists():
        return None
    want = resref.upper()
    for path in folder.iterdir():
        if path.is_file() and path.suffix.lower() == ".wav" and path.stem.upper() == want:
            return path
    return None


def original_wav(resref: str, dest: Optional[Path] = None) -> Optional[Path]:
    """Decoded original game WAV for a SoundResRef, or None."""
    dest = dest or ORIGINAL_DIR
    found = _wav_named(dest, resref)
    if found:
        return found
    if dest.resolve() != MOOD_CLIPS_DIR.resolve():
        return _wav_named(MOOD_CLIPS_DIR, resref)
    return None


_ORIGINAL_MAP_CACHE: Tuple[str, float, Dict[int, str]] = ("", 0.0, {})


def load_original_map(csv_path: Optional[Path] = None) -> Dict[int, str]:
    """strref -> original SoundResRef for every dialog row that already had VO."""
    path = Path(csv_path or cfg.CSV_PATH)
    key = str(path)
    mtime = path.stat().st_mtime if path.exists() else 0.0
    global _ORIGINAL_MAP_CACHE
    cached_key, cached_mtime, cached = _ORIGINAL_MAP_CACHE
    if cached_key == key and cached_mtime == mtime:
        return cached
    mapping: Dict[int, str] = {}
    if path.exists():
        with path.open("r", encoding="utf-8-sig") as handle:
            mapping = {int(r["StrRef"]): r["SoundResRef"]
                       for r in csv.DictReader(handle)
                       if is_original_sound(r.get("SoundResRef") or "")}
    _ORIGINAL_MAP_CACHE = (key, mtime, mapping)
    return mapping


def attach_originals(plan: List[PlanRow], mapping: Optional[Dict[int, str]] = None) -> None:
    """Fill empty PlanRow.original from the dialog report (older plan.csv files)."""
    missing = [p for p in plan if not p.original]
    if not missing:
        return
    mapping = mapping if mapping is not None else load_original_map()
    for row in missing:
        row.original = mapping.get(row.strref, "") or ""


def _chunks(items: Sequence[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


def extract_original_sounds(resrefs: Sequence[str], dest: Optional[Path] = None) -> int:
    """
    Extract original game WAVs (override, then already-collected clips, then BIFFs)
    and decode WAVC in place.

    Args:
        resrefs: Original SoundResRefs (MRT001, ANA001, ...).
        dest: Folder to write ``<RESREF>.WAV``.

    Returns:
        How many resrefs exist as PCM WAV afterwards.
    """
    dest = dest or ORIGINAL_DIR
    unique = list(dict.fromkeys(r.upper() for r in resrefs if r))
    if not unique:
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    override = Path(cfg.GAME_DIRECTORY) / "override"
    missing: List[str] = []
    for resref in unique:
        out = dest / f"{resref}.WAV"
        if out.exists() or _wav_named(dest, resref):
            continue
        src = _wav_named(override, resref) or (
            None if dest.resolve() == MOOD_CLIPS_DIR.resolve() else _wav_named(MOOD_CLIPS_DIR, resref))
        if src is not None:
            out.write_bytes(src.read_bytes())
        else:
            missing.append(resref)

    if missing:
        weidu = Path(cfg.WEIDU_PATH)
        if not weidu.exists():
            logger.warning(f"⚠️ WeiDU not found at {weidu}; {len(missing)} original WAVs not extracted")
        else:
            logger.info(f"Extracting {len(missing)} original voice files with WeiDU...")
            game = str(Path(cfg.GAME_DIRECTORY))
            for batch in _chunks(missing, WEIDU_BATCH):
                cmd = [str(weidu), "--game", game, "--out", str(dest)]
                for resref in batch:
                    cmd += ["--biff-get", f"{resref}.WAV"]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    logger.warning(f"⚠️ WeiDU extract failed: {(result.stderr or result.stdout).strip()[:400]}")

    kept = 0
    for resref in unique:
        path = _wav_named(dest, resref)
        if path is None:
            logger.warning(f"⚠️ Missing original audio {resref}")
            continue
        canonical = dest / f"{resref}.WAV"
        if path.resolve() != canonical.resolve():
            path.replace(canonical)
            path = canonical
        if decode_wavc(path):
            kept += 1
        else:
            path.unlink(missing_ok=True)
            logger.warning(f"⚠️ Could not decode original audio {resref}")
    return kept


def build_plan(rows: List[Dict[str, str]],
               speakers: Optional[Set[str]] = None,
               cast: Optional[Dict[int, Tuple[str, str]]] = None,
               overrides: Optional[Dict[str, Dict]] = None,
               dialog_of: Optional[Dict[int, str]] = None) -> List[PlanRow]:
    """
    List every line that gets a new voice: all spoken lines of characters with
        their own voice (cloned profile or pst-voice-aliases.json), and all lines
        pst_cast.py gave a library voice. Originally voiced lines stay in the
        plan (new VV resref) with OriginalResRef set so generate can keep that
        WAV and only add narrator.

    Args:
        rows: dialog-report CSV rows.
        speakers: Restrict to these RealNames (default: everyone).
        cast: Output of load_cast_voices(); characters with their own voice
            keep it, everyone else uses the cast voice.
        overrides: load_cast_overrides(); hand picks beat both of the above.
        dialog_of: strref -> DLG for dialog-wide overrides.

    Returns:
        The plan, ordered by strref.
    """
    cast = cast or {}
    overrides = overrides or {}
    dialog_of = dialog_of or {}
    own_voice = (profile_voices() | set(load_voice_aliases())) - {NARRATOR}
    plan: List[PlanRow] = []
    for r in rows:
        strref = int(r["StrRef"])
        speaker = r["RealName"]
        voice = ""
        override = cast_override_for(strref, overrides, dialog_of.get(strref, ""))
        if speaker not in own_voice:
            if strref in cast:
                voice, cast_speaker = cast[strref]
                speaker = speaker or cast_speaker
            elif not (override.get("voice") or override.get("speaker") in own_voice):
                continue        # no voice for this speaker yet
        if override:
            row = PlanRow(strref, speaker or dialog_of.get(strref, ""), "generate", "", "", voice)
            apply_cast_override(row, override)
            speaker, voice = row.speaker, row.voice
            if not speaker or (not voice and speaker not in own_voice):
                continue
        if speakers and speaker not in speakers:
            continue
        text = r["Text"]
        if not split_segments(text, voice or speaker):
            continue
        orig = r.get("SoundResRef") or ""
        original = orig if is_original_sound(orig) else ""
        plan.append(PlanRow(strref, speaker, "generate", f"{NEW_RESREF_PREFIX}{strref}", text, voice, original))

    plan.sort(key=lambda p: p.strref)
    return plan


def write_plan(plan: List[PlanRow], path: Path) -> None:
    """Write the plan CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["StrRef", "Speaker", "Action", "SoundResRef", "Text", "Voice", "OriginalResRef"])
        for p in plan:
            writer.writerow([p.strref, p.speaker, p.action, p.resref, p.text, p.voice, p.original])


def _file_mtime(path: Path) -> float:
    """0 when path is missing."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def plan_input_snapshot() -> Dict:
    """Current dialog report / casting / alias files the plan is built from."""
    csv_path = Path(cfg.CSV_PATH)
    return {
        "dialog_csv": str(csv_path),
        "dialog_mtime": _file_mtime(csv_path),
        "casting": str(CASTING_PATH),
        "casting_mtime": _file_mtime(CASTING_PATH),
        "npcs": str(CAST_NPCS_PATH),
        "npcs_mtime": _file_mtime(CAST_NPCS_PATH),
        "aliases": str(VOICE_ALIASES_PATH),
        "aliases_mtime": _file_mtime(VOICE_ALIASES_PATH),
        "cast_overrides": str(CAST_OVERRIDES_PATH),
        "cast_overrides_mtime": _file_mtime(CAST_OVERRIDES_PATH),
    }


def write_plan_meta(plan: List[PlanRow], path: Path = PLAN_META_PATH) -> None:
    """
    Snapshot of the inputs used for this plan, so a later dialog report or
    recast can mark the plan stale.
    """
    meta = plan_input_snapshot()
    meta["lines"] = sum(1 for p in plan if p.action == "generate")
    meta["written_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def load_plan_meta(path: Path = PLAN_META_PATH) -> Optional[Dict]:
    """plan_meta.json, or None if missing / unreadable."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def plan_stale_reasons(meta: Optional[Dict] = None, current: Optional[Dict] = None,
                       plan_path: Path = PLAN_PATH) -> List[str]:
    """
    Why the plan should be rebuilt, or [] if it still matches its inputs.

    A missing plan_meta.json counts as stale so the first Plan click after this
    tracking landed writes the snapshot. mtimes have 1s slack for filesystem noise.
    """
    if not plan_path.exists():
        return ["no plan"]
    meta = meta if meta is not None else load_plan_meta()
    if not meta:
        return ["plan has no input snapshot (click Plan once)"]
    current = current if current is not None else plan_input_snapshot()
    labels = (
        ("dialog_mtime", "dialog report"),
        ("casting_mtime", "casting"),
        ("npcs_mtime", "NPC profiles"),
        ("aliases_mtime", "voice aliases"),
        ("cast_overrides_mtime", "cast overrides"),
    )
    reasons = []
    for key, label in labels:
        stored = float(meta.get(key) or 0)
        now = float(current.get(key) or 0)
        if now and now > stored + 1.0:
            reasons.append(f"{label} is newer than the plan")
    return reasons


def read_plan(path: Path) -> List[PlanRow]:
    """Read the plan CSV. Empty OriginalResRef is filled from the dialog report."""
    with path.open("r", encoding="utf-8-sig") as f:
        plan = [PlanRow(int(r["StrRef"]), r["Speaker"], r["Action"], r["SoundResRef"], r["Text"],
                        r.get("Voice") or "", (r.get("OriginalResRef") or "").strip())
                for r in csv.DictReader(f)]
    attach_originals(plan)
    return plan


# ==================== GENERATION ====================

def _loudnorm_base() -> str:
    """The shared loudnorm target (integrated LUFS, true peak, loudness range)."""
    return f"I={TARGET_LUFS}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"


def measure_loudness(path: Path) -> Optional[Dict[str, str]]:
    """
    First loudnorm pass: measured loudness of a WAV, or None if ffmpeg failed.

    Returns:
        The loudnorm JSON fields (input_i, input_tp, ...) as strings, or None.
    """
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path),
         "-af", f"loudnorm={_loudnorm_base()}:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True)
    match = re.search(r"\{[^{}]*input_i[^{}]*\}", result.stderr, re.S)
    if not match:
        return None
    data = json.loads(match.group(0))
    if str(data.get("input_i", "")).lower() in ("-inf", "inf", "nan"):
        return None
    return {k: str(data[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}


def loudnorm_filter(stats: Optional[Dict[str, str]] = None) -> str:
    """
    An ffmpeg loudnorm filter string at TARGET_LUFS.

    Two-pass (stats from measure_loudness) is linear and accurate on short clips;
    without stats this is the single-pass fallback.
    """
    if not stats:
        return f"loudnorm={_loudnorm_base()}"
    return (f"loudnorm={_loudnorm_base()}:linear=true"
            f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
            f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
            f":offset={stats['target_offset']}")


def stitch(segment_paths: List[Path], out_path: Path) -> bool:
    """
    Join segments with short pauses, normalize each to TARGET_LUFS, write game-quality PCM WAV
    (22.05 kHz mono 16-bit, same as decoded original PST voices). Drama 3's 44.1 kHz is
    resampled down; we do not keep the higher rate in the mod.

    Each segment is loudnorm'd on its own (two-pass when measurement works) so a
    quiet narrator and a loud character land at the same level before they are
    concatenated. The joined file is not loudnorm'd again: equal-LUFS pieces
    already sit at the target.

    Args:
        segment_paths: Segment WAVs in order.
        out_path: Destination WAV.

    Returns:
        True on success.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for p in segment_paths:
        cmd += ["-i", str(p)]
    cmd += ["-f", "lavfi", "-t", str(SEGMENT_GAP), "-i", f"anullsrc=r={SAMPLE_RATE}:cl=mono"]
    gap = len(segment_paths)

    filters = []
    for i, path in enumerate(segment_paths):
        filters.append(f"[{i}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=mono,"
                       f"{loudnorm_filter(measure_loudness(path))}[s{i}]")
    n_gaps = len(segment_paths) - 1
    if n_gaps > 0:
        filters.append(f"[{gap}:a]asplit={n_gaps}" + "".join(f"[g{i}]" for i in range(n_gaps)))
    chain = "".join(f"[s{i}]" + (f"[g{i}]" if i < n_gaps else "") for i in range(len(segment_paths)))
    n_parts = len(segment_paths) + n_gaps
    filters.append(f"{chain}concat=n={n_parts}:v=0:a=1[out]")

    cmd += ["-filter_complex", ";".join(filters), "-map", "[out]",
            "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(out_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"❌ ffmpeg failed for {out_path.name}: {result.stderr.strip()}")
        out_path.unlink(missing_ok=True)
        return False
    return True


# ==================== MOD BUILD ====================

def _tra_string(text: str) -> str:
    """Quote text for a .tra file, picking a delimiter the text doesn't contain."""
    if "~" not in text:
        return f"~{text}~"
    return f"~~~~~{text}~~~~~"


def build_mod(plan: List[PlanRow]) -> None:
    """
    Stage the WeiDU mod: generated WAVs + STRING_SET for every planned strref.

    Every strref gets STRING_SET (text + its new VV<strref> sound), so the
    original voice files stay untouched in the game.

    Args:
        plan: The revoice plan.
    """
    audio_out = MOD_DIR / MOD_NAME / "audio"
    tra_out = MOD_DIR / MOD_NAME / "tra"
    audio_out.mkdir(parents=True, exist_ok=True)
    tra_out.mkdir(parents=True, exist_ok=True)

    rows: List[PlanRow] = []
    skipped = 0
    for p in plan:
        if p.action == "generate":
            src = FISH_AUDIO_DIR / f"{p.resref}.WAV"
            if not src.exists():
                skipped += 1
                continue
            (audio_out / src.name).write_bytes(src.read_bytes())
        rows.append(p)

    # Info-point examine lines and BCS idle barks keep their dialog.tlk text;
    # GemRB sync only patches the sound resref. WeiDU still copies their WAVs.
    from pst_floats import is_float
    tra_rows = [p for p in rows if not p.is_infopoint and not is_float(p)]

    encoding = cfg.TEXT_ENCODING or "utf-8"
    with (tra_out / "voices.tra").open("w", encoding=encoding, errors="replace", newline="\n") as f:
        for p in tra_rows:
            f.write(f"@{p.strref} = {_tra_string(p.text)} [{p.resref}]\n")

    tp2 = [
        f"BACKUP ~{MOD_NAME}/backup~",
        "AUTHOR ~VVE Builder~",
        "VERSION ~v1.0~",
        "",
        "// Replaces every voice in the game with newly generated audio.",
        "BEGIN ~PST Revoiced: new voices for every character~",
        f"COPY ~{MOD_NAME}/audio~ ~override~",
        f"LOAD_TRA ~{MOD_NAME}/tra/voices.tra~",
    ] + [f"STRING_SET {p.strref} @{p.strref}" for p in tra_rows]
    (MOD_DIR / f"setup-{MOD_NAME}.tp2").write_text("\n".join(tp2) + "\n", encoding="utf-8")

    weidu = Path(cfg.WEIDU_PATH)
    if weidu.exists():
        (MOD_DIR / f"setup-{MOD_NAME}.exe").write_bytes(weidu.read_bytes())

    logger.info(f"📦 Staged {MOD_DIR}: {len(rows)} lines, {skipped} without audio yet (left out)")


# ==================== MAIN ====================

def rebuild_plan(speakers: Optional[Set[str]] = None, extract_originals: bool = True) -> List[PlanRow]:
    """
    Rebuild and write plan.csv + plan_meta.json from the dialog report, casting
    and pst-cast-overrides.json (examine and idle-bark rows are kept), then make
    sure original game WAVs exist. Used by `plan` and by the GUI after a recast
    (which passes extract_originals=False: a recast never adds original VO).
    """
    with open(cfg.CSV_PATH, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    overrides = load_cast_overrides()
    dialog_of = dialog_of_strrefs()
    plan = build_plan(rows, speakers, load_cast_voices(), overrides, dialog_of)
    if PLAN_PATH.exists():
        from pst_floats import keep_existing as keep_floats
        from pst_infopoints import keep_existing as keep_infopoints
        previous = read_plan(PLAN_PATH)
        plan = keep_infopoints(plan, previous)
        plan = keep_floats(plan, previous)
    hand_picked = apply_cast_overrides(plan, overrides, dialog_of)
    if hand_picked:
        logger.info(f"\u270b {hand_picked} lines use a hand-picked speaker/voice ({CAST_OVERRIDES_PATH})")
    write_plan(plan, PLAN_PATH)
    write_plan_meta(plan)
    originals = sorted({p.original for p in plan if p.original})
    if originals and extract_originals:
        extract_original_sounds(originals)
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="List every line that gets a new voice")
    p_plan.add_argument("--speaker", action="append", help="Only this character (repeatable)")

    sub.add_parser("build", help="Stage the WeiDU mod")
    args = parser.parse_args()

    if args.command == "plan":
        plan = rebuild_plan(set(args.speaker) if args.speaker else None)
        speakers = sorted({p.speaker for p in plan if not p.voice})
        cast_lines = [p for p in plan if p.voice]
        originals = sorted({p.original for p in plan if p.original})
        keep = sum(1 for p in plan if p.original)
        mixed = sum(1 for p in plan if p.original and has_narration(p.text, p.speaker))
        logger.info(f"📋 {PLAN_PATH}: {len(plan)} lines to generate, "
                    f"{len(speakers)} characters with their own voice: {', '.join(speakers)}")
        logger.info(f"🎭 {len(cast_lines)} lines from {len({p.speaker for p in cast_lines})} cast NPCs "
                    f"using {len({p.voice for p in cast_lines})} library voices")
        if originals:
            kept = sum(1 for r in originals if _wav_named(ORIGINAL_DIR, r))
            logger.info(f"🎧 {keep} lines keep original VO ({kept}/{len(originals)} unique files in "
                        f"{ORIGINAL_DIR})"
                        + (f", {mixed} with narrator to augment" if mixed else ""))
        return

    plan = read_plan(PLAN_PATH)
    if args.command == "build":
        build_mod(plan)


if __name__ == "__main__":
    main()
