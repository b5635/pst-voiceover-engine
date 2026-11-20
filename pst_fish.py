#!/usr/bin/env python3
"""
PST Fish Audio Drama 3 Generator
Generates the new voice lines with Fish Audio Drama 3 (speech.cpp, libs/tts_drama3.py).
Uses the revoice plan, text overrides, pronunciation rules, mood references
and stitching. OmniVoice is not part of this path.

Originally voiced lines keep the game WAV (pst_revoice/original/). Drama 3
only speaks narrator on those lines; the two are stitched. Silent lines are
fully generated:

    - a voice file, encoded once into voices_drama3_pst/: by default one
      multi-reference voice per character (their default clip plus their
      most-used mood clips from voices_moods_pst/, up to ~30 s), the narrator
      clip, or the cast library voice; --voice-mode mood uses the line's mood
      clip as OmniVoice does
    - the text, with *emphasis* turned into <emphasis>...</emphasis>
    - a performance direction: the director's (pst_direct.py, Strata LLM ->
      pst_revoice/directions.json) when it was written for this exact text,
      otherwise a short one from the line's mood, e.g. "[dry, sarcastic]"

    python pst_fish.py --speaker Annah --limit 10
    python pst_fish.py --strref 7711 --strref 30831 --force
    python pst_fish.py --strref 7711 --directions pst_revoice/directions_claude.json --out pst_revoice/audio_fish_claude
    python pst_fish.py --compare            # only rewrite pst_revoice/compare.html

Output: pst_revoice/audio_fish/<RESREF>.WAV (+ generation.json: speaker, voice, spoken
text, TTS segments, pronunciation, directed texts). --stale regenerates WAVs whose
snapshot no longer matches. pst_revoice/compare.html lists OmniVoice vs every Drama 3
output folder.
"""

import argparse
import html
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from libs.drama3_takes import (  # noqa: F401  (re-exported: tests and pst_direct use them from here)
    ATTEMPTS,
    BRACKET_RE,
    CHUNK_WORDS,
    MAX_BUDGET,
    MAX_DIRECTION_WORDS,
    MAX_INNER_SILENCE,
    MIN_SECONDS_PER_WORD,
    PAIRED_RE,
    SECONDS_PER_WORD,
    SILENCE_DB,
    WORD_RE,
    _balanced,
    chunk_text,
    leading_direction,
    longest_inner_silence,
    seed_for,
    speak_segment,
    spoken_words,
    take_problem,
    time_budget,
    trim_silence,
    with_direction,
)
from libs.pst_references import load_library, load_moods, reference_for
from libs.tts_drama3 import Drama3Worker, Take, WorkerError, build_voice
from libs.utils import load_patcher_config, setup_logging
from pst_revoice import (
    FISH_AUDIO_DIR,
    NARRATOR,
    PLAN_PATH,
    PlanRow,
    attach_originals,
    extract_original_sounds,
    generation_snapshot,
    load_generation,
    load_line_overrides,
    load_pronunciation,
    original_wav,
    read_plan,
    stitch,
    tts_segments,
)

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

logger = setup_logging(Path(__file__).stem)

# ==================== CONFIGURATION ====================
OUT_DIR = FISH_AUDIO_DIR
OMNI_DIR = Path("pst_revoice/_archive_omnivoice/audio_omnivoice")  # archived; compare.html only
DIRECTIONS_PATH = Path("pst_revoice/directions.json")   # pst_direct.py
VOICES_DIR = Path("voices_drama3_pst")                   # cached .voice.gguf per reference clip
COMPARE_PATH = Path("pst_revoice/compare.html")
PATCHER_CONFIG_PATH = Path("patcher-config-pst.json")

NARRATOR_DIRECTION = "calm, measured storyteller voice"
# Used when the director has not written a direction for the line's current text
MOOD_DIRECTIONS = {
    "neutral": "",
    "warm": "warm, gentle",
    "playful": "amused, teasing",
    "sarcastic": "dry, sarcastic",
    "angry": "angry, heated",
    "sad": "quiet, sorrowful",
    "fearful": "nervous, anxious",
    "solemn": "grave, measured",
}
MULTI_REFERENCE_SECONDS = 30.0   # cap on a multi-reference voice (default clip + most-used moods)
PAIRED_TAGS = ("whisper", "emphasis", "soft", "fast", "slow", "stress", "forceful")
MAX_POINT_TAGS = 2          # [sighs]-style events per segment; more tends to drop words or stall
DIRECTOR_PROMPT_VERSION = 2  # bump when pst_direct's prompt or delivery guard changes: Strata directions redo
ALL_CAPS_RATIO = 0.85       # a segment this upper-case is put in sentence case (sentence_case)

# =======================================================



# ==================== DRAMA 3 TEXT ====================

def sentence_case(text: str) -> str:
    """
    ALL-CAPS speech (the Transcendent One) in sentence case: Drama 3 reads caps
    as shouting or spells short words out. Tags are left alone.

    Example:
        >>> sentence_case("SO YOU HAVE COME. I KNOW YOUR HEART, FALLEN TANAR'RI.")
        "So you have come. I know your heart, fallen tanar'ri."
    """
    out = re.sub(r"(<[^>]*>)|([^<]+)", lambda m: m.group(1) or m.group(2).lower(), text)
    out = re.sub(r"\bi\b", "I", out)
    return re.sub(r"(^|[.!?]\s+|\.\.\.\s*|-\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), out)


def emphasis_tags(text: str) -> str:
    """
    Turn emphasised words into Drama 3 <emphasis> spans.

    tts_segments() leaves PST's *word* emphasis as "word" (patcher-config rule),
    and a segment holds no other double quotes, so each quoted run is an emphasis.
    Shouted ALL-CAPS words in otherwise mixed-case text ("A LOT better") are
    lowercased into an emphasis too, so they are not spelled out letter by letter;
    a mostly upper-case text (the Transcendent One) is left alone.

    Example:
        >>> emphasis_tags('Why don\\'t you "try" and split my jaw')
        "Why don't you <emphasis>try</emphasis> and split my jaw"
        >>> emphasis_tags("I like this you. A LOT better.")
        'I like this you. A <emphasis>lot</emphasis> better.'
    """
    text = re.sub(r'"([^"]+)"', r"<emphasis>\1</emphasis>", text).replace('"', "")
    letters = [c for c in PAIRED_RE.sub("", text) if c.isalpha()]
    if len(letters) >= 3 and sum(c.isupper() for c in letters) / len(letters) >= ALL_CAPS_RATIO:
        return tidy_emphasis(sentence_case(text))
    if letters and sum(c.isupper() for c in letters) / len(letters) < 0.5:
        text = re.sub(r"\b[A-Z]{2,}(?:\s+[A-Z]{2,})*\b",
                      lambda m: f"<emphasis>{m.group(0).lower()}</emphasis>", text)
    return tidy_emphasis(text)


def _flatten_emphasis(text: str) -> str:
    """Keep only the outermost level of nested <emphasis> spans; drop stray closers."""
    out, depth, pos = [], 0, 0
    for m in re.finditer(r"</?emphasis>", text):
        out.append(text[pos:m.start()])
        pos = m.end()
        if m.group(0) == "<emphasis>":
            if depth == 0:
                out.append(m.group(0))
            depth += 1
        elif depth > 0:
            depth -= 1
            if depth == 0:
                out.append(m.group(0))
    out.append(text[pos:])
    if depth > 0:
        out.append("</emphasis>")
    return "".join(out)


def tidy_emphasis(text: str) -> str:
    """
    Repair <emphasis> spans the game's *asterisks* produce: "they'<emphasis>re"
    splits ("*they*'*re*"), nested spans ("**kliks**", Vhailor's *speech* with
    *emphasis* inside), a span over the whole segment (emphasising everything
    is emphasising nothing), and a capital 'S right after a span ("*everybody*'S").

    Example:
        >>> tidy_emphasis("<emphasis>they</emphasis>'<emphasis>re</emphasis> dead")
        "<emphasis>they're</emphasis> dead"
    """
    text = text.replace("</emphasis>'<emphasis>", "'")
    outer = re.fullmatch(r"\s*<emphasis>(.*)</emphasis>\s*", text, flags=re.S)
    if outer and _balanced(outer.group(1)):
        text = outer.group(1)
    text = _flatten_emphasis(text)
    text = re.sub(r"</emphasis>'S\b", "</emphasis>'s", text)
    whole = re.fullmatch(r"\s*<emphasis>((?:(?!</?emphasis>).)*)</emphasis>\s*", text)
    return whole.group(1) if whole else text


STUTTER_RE = re.compile(r"\b(?!h[aeiou]+h?-)([a-z]{1,3})(?:-\1)+-(?=[a-z])(?!\1\b)", re.I)


def collapse_stutters(text: str) -> str:
    """
    Cut every written stutter to one repeat: Drama 3 reads "y-y-y-years" as
    letter names ("why why why"). Laughs ("Hee-hee-heeee") and whole-word
    repeats ("tkk-tkk-tkk", "are-are-are") are left alone.

    Example:
        >>> collapse_stutters("That b-b-b-bubber! Y-y-y-years ago, muh-muh-memories")
        'That b-bubber! Y-years ago, muh-memories'
    """
    return STUTTER_RE.sub(r"\1-", text)


def drama3_segments(text: str, row: PlanRow, patcher_config: Dict,
                    pronunciation: Dict[str, List[Dict[str, str]]]) -> List[Tuple[str, str]]:
    """(voice, base text) per segment: what OmniVoice would speak, stutters cut to one repeat, with emphasis tags."""
    return [(voice, emphasis_tags(collapse_stutters(seg)))
            for voice, seg in tts_segments(text, row.speaker, patcher_config, pronunciation, row.voice)]


LIST_OPEN_RE = re.compile(r"""^\s*\[\s*['"]\s*,?\s*""")
LIST_CLOSE_RE = re.compile(r"""\s*['"]\s*\]\s*$""")


def unwrap_list_text(directed: str) -> str:
    """
    Undo a director reply that wrapped its text like a Python list item.

    Example:
        >>> unwrap_list_text("['angry] Go!']")
        '[angry] Go!'
    """
    if LIST_OPEN_RE.match(directed):
        directed = "[" + LIST_OPEN_RE.sub("", directed, count=1)
        directed = LIST_CLOSE_RE.sub("", directed)
    elif re.match(r"""^\s*['"]\[""", directed) and re.search(r"""\]?['"]\s*$""", directed):
        directed = re.sub(r"""^\s*['"]""", "", re.sub(r"""['"]\s*$""", "", directed))
    return directed


def sanitize_directed(base: str, directed: str) -> Tuple[str, bool]:
    """
    Make a director's text safe to speak.

    Unknown <tags> are dropped, extra [events] beyond MAX_POINT_TAGS removed and
    the leading direction shortened. If the words no longer match `base`
    exactly, or the paired tags don't nest, only the leading direction is kept
    and put in front of `base`.

    Args:
        base: The segment's text as it would be spoken (drama3_segments()).
        directed: The director's version of it, with tags added.

    Returns:
        (text to speak, True if the director's tags were kept).

    Example:
        >>> sanitize_directed("Git!", "[hissing] Git!")
        ('[hissing] Git!', True)
        >>> sanitize_directed("Git!", "[hissing] Get out!")
        ('[hissing] Git!', False)
    """
    directed = unwrap_list_text(directed)
    direction = leading_direction(directed)
    text = PAIRED_RE.sub(lambda m: m.group(0) if m.group(1) in PAIRED_TAGS else "", directed)
    text = re.sub(r"^\s*\[[^\[\]]*\]", "", text, count=1)
    events = 0

    def keep_event(match: "re.Match[str]") -> str:
        nonlocal events
        if not any(c.isalpha() for c in match.group(0)):
            return ""
        events += 1
        return match.group(0) if events <= MAX_POINT_TAGS else ""

    text = re.sub(r"\s+", " ", BRACKET_RE.sub(keep_event, text)).strip()
    if spoken_words(text) != spoken_words(base) or not _balanced(text):
        return with_direction(direction, base), False
    return with_direction(direction, text), True


def load_directions(path: Path = DIRECTIONS_PATH) -> Dict[int, Dict]:
    """Read director output: {strref: {"mood", "segments": [{"voice", "base", "text"}], ...}}."""
    if not path.exists():
        return {}
    return {int(k): v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if not k.startswith("_")}


def save_directions(directions: Dict[int, Dict], path: Path = DIRECTIONS_PATH) -> None:
    """Write director output keyed by strref."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({str(k): v for k, v in sorted(directions.items())}, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def directed_segments(row: PlanRow, text: str, patcher_config: Dict,
                      pronunciation: Dict[str, List[Dict[str, str]]], directions: Dict[int, Dict],
                      mood: str) -> List[Dict[str, str]]:
    """
    The segments Drama 3 speaks for one line.

    A director's segment is used only if it was written for exactly this base
    text, so editing the line or its pronunciation never speaks stale words.

    Returns:
        [{"voice", "base", "text", "source"}], source "director", "director-direction-only",
        "mood" or "narrator".
    """
    entry = directions.get(row.strref, {})
    written = {s.get("base"): s.get("text", "") for s in entry.get("segments", [])}
    result = []
    for voice, base in drama3_segments(text, row, patcher_config, pronunciation):
        if voice == NARRATOR:
            spoken, source = with_direction(NARRATOR_DIRECTION, base), "narrator"
        elif base in written:
            spoken, kept = sanitize_directed(base, written[base])
            source = "director" if kept else "director-direction-only"
        else:
            spoken, source = with_direction(MOOD_DIRECTIONS.get(mood, ""), base), "mood"
        result.append({"voice": voice, "base": base, "text": spoken, "source": source})
    return result


def planned_audio_segments(row: PlanRow, text: str, patcher_config: Dict,
                           pronunciation: Dict[str, List[Dict[str, str]]],
                           directions: Dict[int, Dict], mood: str) -> List[Dict[str, str]]:
    """
    What generate will stitch for one line, in order.

    When the original game WAV is on disk, character speech uses that file
    (once per line: the actor's take covers every quote). Narrator segments
    are still Drama 3. A voiced line that our splitter treats as all narration
    still keeps the original WAV as the whole line, so we do not speak the
    same words twice.
    """
    segs = directed_segments(row, text, patcher_config, pronunciation, directions, mood)
    path = original_wav(row.original) if row.original else None
    if not path:
        return segs
    has_speech = any(s["voice"] != NARRATOR for s in segs)
    has_narr = any(s["voice"] == NARRATOR for s in segs)
    original_seg = {
        "voice": row.speech_voice, "base": "", "text": "", "source": "original",
        "resref": row.original.upper(),
    }
    if not (has_speech and has_narr):
        return [original_seg]
    out: List[Dict[str, str]] = []
    used = False
    for seg in segs:
        if seg["voice"] != NARRATOR:
            if not used:
                out.append({**original_seg, "voice": seg["voice"], "base": seg.get("base", "")})
                used = True
            continue
        out.append(seg)
    return out


def _audio_fingerprint(segments: List[Dict]) -> List[Tuple]:
    """Comparable stitch plan: original resrefs and TTS texts in order."""
    fp = []
    for seg in segments:
        if seg.get("source") == "original":
            fp.append(("original", (seg.get("resref") or "").upper()))
        else:
            fp.append(("tts", seg.get("voice", ""), seg.get("text", "")))
    return fp


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def line_mood(row: PlanRow, directions: Dict[int, Dict], picks: Optional[Dict[int, Dict]] = None) -> str:
    """Classifier mood for the line, falling back to the director's pick."""
    picks = picks or {}
    return (picks.get(row.strref) or {}).get("Mood") or (directions.get(row.strref) or {}).get("mood") or ""


def direction_fields(row: PlanRow, spoken: str, patcher_config: Dict,
                     pronunciation: Dict[str, List[Dict[str, str]]]) -> Dict:
    """
    Snapshot stored on a directions.json entry so later spoken / speaker /
    pronunciation edits can mark the direction stale ("voice" is informational).
    """
    snap = generation_snapshot(row, spoken, patcher_config, pronunciation)
    return {
        "speaker": row.speaker,
        "voice": row.speech_voice,
        "spoken": spoken,
        "rules": snap["rules"],
        "rules_fingerprint": snap["rules_fingerprint"],
        "directed_at": _utc_now(),
    }


def direction_stale_reasons(row: PlanRow, spoken: str, patcher_config: Dict,
                            pronunciation: Dict[str, List[Dict[str, str]]],
                            entry: Optional[Dict]) -> List[str]:
    """
    Why a cached direction no longer matches this line, or [] if it is current
    (or there is no speech to direct). Originally voiced lines keep the game WAV
    and are not directed.
    """
    if row.original or not entry:
        return []
    bases = [base for voice, base in drama3_segments(spoken, row, patcher_config, pronunciation)
             if voice != NARRATOR]
    if not bases:
        return []
    reasons = []
    if (str(entry.get("source", "")).startswith("strata")
            and int(entry.get("prompt_version") or 1) < DIRECTOR_PROMPT_VERSION):
        reasons.append("directed with an older director prompt")
    # A voice-only recast keeps the direction: it is written for the character and
    # the moment, not the clone. The audio still goes stale (audio_stale_reasons).
    if entry.get("speaker") and entry["speaker"] != row.speaker:
        reasons.append(f"speaker changed (was {entry['speaker']})")
    spoken_changed = "spoken" in entry and entry["spoken"] != spoken
    if spoken_changed:
        reasons.append("spoken text changed")
    stored_bases = [seg.get("base", "") for seg in entry.get("segments", [])]
    if stored_bases != bases:
        if spoken_changed:
            pass
        elif "spoken" in entry:
            reasons.append("speech bases changed (pronunciation)")
        else:
            reasons.append("speech bases changed (dialogue or pronunciation)")
    return reasons


def generation_record(row: PlanRow, spoken: str, patcher_config: Dict,
                      pronunciation: Dict[str, List[Dict[str, str]]],
                      directions_path: Path, direction_entry: Optional[Dict],
                      mood: str, voice_mode: str, segments: List[Dict]) -> Dict:
    """
    What Drama 3 was given for one line: speaker, voice, spoken text, TTS
    segments, matching pronunciation rules, and the directed texts actually sent.
    """
    snap = generation_snapshot(row, spoken, patcher_config, pronunciation)
    entry = direction_entry or {}
    original = next((str(s.get("resref") or "").upper() for s in segments if s.get("source") == "original"), "")
    return {
        **snap,
        "original": original,
        "mood": mood or "-",
        "voice_mode": voice_mode,
        "directions": str(directions_path),
        "director": entry.get("source", ""),
        "generated_at": _utc_now(),
        "segments": segments,
    }


def audio_stale_reasons(row: PlanRow, spoken: str, patcher_config: Dict,
                        pronunciation: Dict[str, List[Dict[str, str]]],
                        directions: Dict[int, Dict], mood: str,
                        record: Optional[Dict], voice_mode: str = "multi") -> List[str]:
    """
    Why existing Drama 3 audio should be regenerated, or [] if it still matches.

    Covers speaker, library voice, spoken text, pronunciation (via directed
    bases / stored tts), performance direction, voice mode, and whether the
    line still keeps original game audio. A WAV with no generation.json record
    is always stale so the next generate writes a snapshot.
    """
    if not record:
        return ["no generation.json record"]
    planned = planned_audio_segments(row, spoken, patcher_config, pronunciation, directions, mood)
    uses_original = any(s.get("source") == "original" for s in planned)
    stored_original = any(s.get("source") == "original" for s in record.get("segments") or [])
    if uses_original or stored_original:
        reasons = []
        if _audio_fingerprint(planned) != _audio_fingerprint(record.get("segments") or []):
            if uses_original and not stored_original:
                reasons.append("now keeps original audio")
            elif stored_original and not uses_original:
                reasons.append("original audio no longer used")
            elif (record.get("original") or "").upper() != (row.original or "").upper():
                reasons.append(f"original resref changed (was {record.get('original') or 'none'})")
            else:
                reasons.append("directed text changed")
        if record.get("spoken") not in (None,) and record["spoken"] != spoken:
            reasons.append("spoken text changed")
        return reasons
    reasons = []
    if record.get("speaker") and record["speaker"] != row.speaker:
        reasons.append(f"speaker changed (was {record['speaker']})")
    stored_voice = record.get("voice")
    if stored_voice not in (None, "") and stored_voice != row.speech_voice:
        reasons.append(f"voice changed (was {stored_voice})")
    if record.get("spoken") not in (None,) and record["spoken"] != spoken:
        reasons.append("spoken text changed")
    stored_texts = [seg.get("text", "") for seg in record.get("segments", [])]
    if stored_texts != [seg["text"] for seg in planned]:
        reasons.append("directed text changed")
    stored_mode = record.get("voice_mode")
    if stored_mode not in (None, "") and stored_mode != voice_mode:
        reasons.append(f"voice mode changed (was {stored_mode})")
    return reasons


# ==================== GENERATION ====================

@dataclass
class Job:
    """One segment to speak, or an original game WAV to stitch in."""
    row: PlanRow
    index: int
    voice: str
    text: str
    ref_key: str
    mood: str
    source: str
    take: Optional[Take] = None
    attempts: List[str] = field(default_factory=list)
    original_path: Optional[Path] = None


def voice_key(voice: str, mood: str) -> str:
    """Worker voice name / cache file stem for a reference."""
    name = voice if mood in ("-", voice) else f"{voice}.{mood}"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def speak(worker: Drama3Worker, job: Job) -> None:
    """Speak a job (libs.drama3_takes.speak_segment) into job.take / job.attempts."""
    job.take, job.attempts = speak_segment(worker, job.row.strref, job.index, job.ref_key, job.text)


def multi_reference_moods(voice: str, entry: Dict, picks: Dict[int, Dict[str, str]]) -> List[str]:
    """
    The mood clips that make up a character's multi-reference voice.

    The default clip first, then the moods the character's lines use most, while
    the total stays within MULTI_REFERENCE_SECONDS (Drama 3 clones best from about
    10-30 s of reference; every request's prompt carries all of it).
    """
    usage: Dict[str, int] = {}
    for pick in picks.values():
        if pick.get("Speaker") == voice:
            usage[pick.get("Mood", "")] = usage.get(pick.get("Mood", ""), 0) + 1
    others = sorted((m for m in entry["moods"] if m != entry["default"]), key=lambda m: -usage.get(m, 0))
    chosen, total = [entry["default"]], float(entry["moods"][entry["default"]].get("seconds", 0))
    for mood in others:
        seconds = float(entry["moods"][mood].get("seconds", 0))
        if total + seconds <= MULTI_REFERENCE_SECONDS:
            chosen.append(mood)
            total += seconds
    return chosen


def character_reference(voice: str, strref: int, manifest: Dict, picks: Dict[int, Dict[str, str]],
                        voice_mode: str) -> Optional[Tuple[str, str, List[Tuple[Path, str]]]]:
    """
    (voice key, mood, recordings) for a character with mood references, by voice mode.

    - "multi": one voice per character from several mood clips (multi_reference_moods), so the
      model hears the character's range and speech.cpp reuses one cached reference prompt for
      all of their lines; the direction carries each line's emotion
    - "mood": the clip of the mood picked for the line (as OmniVoice does)
    - "default": always the character's default clip

    Returns None for a voice that has no mood references (narrator, library voices).
    """
    entry = manifest.get(voice)
    if not entry:
        return None
    mood = picks.get(strref, {}).get("Mood", entry["default"])
    mood = mood if mood in entry["moods"] else entry["default"]
    if voice_mode == "mood":
        info = entry["moods"][mood]
        return voice_key(voice, mood), mood, [(Path(info["wav"]), info["text"])]
    if voice_mode == "default":
        info = entry["moods"][entry["default"]]
        return voice_key(voice, entry["default"]), mood, [(Path(info["wav"]), info["text"])]
    moods = multi_reference_moods(voice, entry, picks)
    return (voice_key(voice, "multi"), mood,
            [(Path(entry["moods"][m]["wav"]), entry["moods"][m]["text"]) for m in moods])


def build_jobs(rows: List[PlanRow], directions: Dict[int, Dict],
               voice_mode: str = "multi") -> Tuple[List[Job], Dict[str, List[Tuple[Path, str]]]]:
    """Segments to speak and the references (voice key -> recordings) they need."""
    manifest, picks = load_moods()
    # A line the classifier never saw uses the director's mood pick
    picks = {**{s: {"Mood": d["mood"]} for s, d in directions.items() if d.get("mood")}, **picks}
    library = load_library()
    patcher_config = load_patcher_config(PATCHER_CONFIG_PATH)
    pronunciation = load_pronunciation()
    overrides = load_line_overrides()
    jobs: List[Job] = []
    refs: Dict[str, List[Tuple[Path, str]]] = {}
    for row in rows:
        mood = picks.get(row.strref, {}).get("Mood", "")
        segments = planned_audio_segments(row, overrides.get(row.strref, row.text), patcher_config, pronunciation,
                                          directions, mood)
        for i, seg in enumerate(segments):
            if seg.get("source") == "original":
                path = original_wav(seg.get("resref") or row.original)
                if path is None:
                    logger.warning(f"⚠️ {row.strref}: original {row.original} missing")
                    continue
                jobs.append(Job(row, i, seg["voice"], "", "", "-", "original", original_path=path))
                continue
            character = character_reference(seg["voice"], row.strref, manifest, picks, voice_mode)
            if character:
                key, ref_mood, recordings = character
            else:
                ref = reference_for(seg["voice"], row.strref, manifest, picks, library)
                if ref is None:
                    logger.warning(f"⚠️ {row.strref}: no reference for {seg['voice']}")
                    continue
                wav, transcript, ref_mood = ref
                if not transcript:
                    logger.warning(f"⚠️ {row.strref}: {seg['voice']} has no transcript (Drama 3 needs one); skipped")
                    continue
                key, recordings = voice_key(seg["voice"], ref_mood), [(wav, transcript)]
            refs[key] = recordings
            jobs.append(Job(row, i, seg["voice"], seg["text"], key, ref_mood, seg["source"]))
    return jobs, refs


def write_generation(out_dir: Path, records: Dict[int, Dict]) -> None:
    """Merge per-line records (what Drama 3 was given) into <out_dir>/generation.json."""
    path = out_dir / "generation.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.update({str(k): v for k, v in records.items()})
    path.write_text(json.dumps(dict(sorted(data.items(), key=lambda kv: int(kv[0]))), indent=2, ensure_ascii=False),
                    encoding="utf-8")


def select_todo(rows: List[PlanRow], out_dir: Path, directions_path: Path, limit: int = 0, force: bool = False,
                voice_mode: str = "multi", stale: bool = False, quiet: bool = False) -> List[PlanRow]:
    """
    The rows generate would (re)speak: no WAV yet, or (with stale) a WAV whose
    generation.json snapshot no longer matches. Attaches original game audio
    and extracts the original WAVs those rows need.
    """
    directions = load_directions(directions_path)
    patcher_config = load_patcher_config(PATCHER_CONFIG_PATH)
    pronunciation = load_pronunciation()
    overrides = load_line_overrides()
    records_on_disk = load_generation(out_dir / "generation.json")
    _, picks = load_moods()
    attach_originals(rows)
    todo: List[PlanRow] = []
    skipped_stale: List[Tuple[PlanRow, List[str]]] = []
    skipped_ok = 0
    for p in rows:
        wav = out_dir / f"{p.resref}.WAV"
        if force or not wav.exists():
            todo.append(p)
            continue
        spoken = overrides.get(p.strref, p.text)
        mood = line_mood(p, directions, picks)
        reasons = audio_stale_reasons(p, spoken, patcher_config, pronunciation, directions, mood,
                                      records_on_disk.get(p.strref), voice_mode)
        if reasons and stale:
            todo.append(p)
        elif reasons:
            skipped_stale.append((p, reasons))
        else:
            skipped_ok += 1
    if limit:
        todo = todo[:limit]
    orig_resrefs = [p.original for p in todo if p.original]
    if orig_resrefs:
        kept = extract_original_sounds(orig_resrefs)
        logger.info(f"🎧 Original VO: {kept} files ready for {len(orig_resrefs)} lines")
    if quiet:
        return todo
    if skipped_stale:
        logger.info(f"⏭️ {len(skipped_stale)} existing WAVs are stale "
                    f"(spoken/direction/speaker/voice/pronunciation). Re-run with --stale to regenerate them.")
        for p, reasons in skipped_stale[:15]:
            logger.info(f"   ⚠ {p.speaker} {p.strref}: {'; '.join(reasons)}")
        extra = len(skipped_stale) - 15
        if extra > 0:
            logger.info(f"   … {extra} more")
    if skipped_ok:
        logger.info(f"⏭️ Skipping {skipped_ok} up-to-date lines")
    return todo


def build_voice_files(refs: Dict[str, List[Tuple[Path, str]]]) -> Dict[str, Path]:
    """Encode (or reuse) the .voice.gguf for each reference key."""
    return {key: build_voice(recordings, VOICES_DIR / f"{key}.voice.gguf") for key, recordings in refs.items()}


def stitch_lines(todo: List[PlanRow], jobs: List[Job], out_dir: Path, directions_path: Path,
                 voice_mode: str = "multi", segment_dir: Optional[Path] = None) -> int:
    """
    Stitch every row whose segments are all ready (job.take set, or an original
    clip) from <segment_dir>/<RESREF>_<index>.wav into <out_dir>/<RESREF>.WAV,
    and merge their records into generation.json.

    Returns:
        Number of lines written.
    """
    directions = load_directions(directions_path)
    patcher_config = load_patcher_config(PATCHER_CONFIG_PATH)
    pronunciation = load_pronunciation()
    overrides = load_line_overrides()
    _, picks = load_moods()
    segment_dir = segment_dir or out_dir / "segments"
    by_row: Dict[int, List[Job]] = {}
    for job in jobs:
        by_row.setdefault(job.row.strref, []).append(job)

    def _ready(job: Job) -> bool:
        return job.original_path is not None or job.take is not None

    def _job_record(job: Job) -> Dict:
        if job.original_path is not None:
            return {"voice": job.voice, "text": "", "source": "original",
                    "resref": job.row.original.upper()}
        return {"voice": job.voice, "reference": job.ref_key, "text": job.text, "source": job.source,
                "seed": job.take.seed, "seconds": round(job.take.seconds, 2), "attempts": job.attempts}

    written = 0
    records: Dict[int, Dict] = {}
    for idx, row in enumerate(todo, 1):
        row_jobs = sorted(by_row.get(row.strref, []), key=lambda j: j.index)
        out = out_dir / f"{row.resref}.WAV"
        if not row_jobs or any(not _ready(j) for j in row_jobs):
            logger.warning(f"[{idx}/{len(todo)}] ⚠️ {row.speaker} {row.strref} failed")
            continue
        paths = [j.original_path if j.original_path is not None
                 else segment_dir / f"{row.resref}_{j.index}.wav" for j in row_jobs]
        if not stitch(paths, out):
            continue
        speech_moods = [j.mood for j in row_jobs if j.voice != NARRATOR and j.source != "original"]
        spoken = overrides.get(row.strref, row.text)
        mood = speech_moods[0] if speech_moods else line_mood(row, directions, picks)
        records[row.strref] = generation_record(
            row, spoken, patcher_config, pronunciation, directions_path, directions.get(row.strref),
            mood, voice_mode, [_job_record(j) for j in row_jobs],
        )
        written += 1
        tts_attempts = [j.attempts for j in row_jobs if j.original_path is None]
        flags = "" if not tts_attempts or all(a[-1] == "ok" for a in tts_attempts) else " (kept a flawed take)"
        kept = " original" if any(j.original_path is not None for j in row_jobs) else ""
        logger.info(f"[{idx}/{len(todo)}] ✅ {row.speaker} {row.strref} ({records[row.strref]['mood']}"
                    f"{kept}) -> {out.name}{flags}")
    if records:
        write_generation(out_dir, records)
    return written


def generate(rows: List[PlanRow], out_dir: Path, directions_path: Path, limit: int = 0, force: bool = False,
             threads: Optional[int] = None, voice_mode: str = "multi", stale: bool = False,
             device: Optional[str] = None) -> int:
    """
    Generate and stitch each row on this PC (pst_farm.py spreads the same work over several PCs).

    Originally voiced lines keep the game WAV and only send narrator (if any)
    to Drama 3. Silent lines are fully generated. Skips finished files unless
    force. With stale, also regenerates WAVs whose generation.json snapshot no
    longer matches speaker, voice, spoken text, pronunciation, directed text,
    or original-audio choice.

    TTS segments are spoken grouped by voice, so speech.cpp reuses each
    reference prompt, then every line is stitched like pst_revoice.py does.

    Returns:
        Number of lines written.
    """
    todo = select_todo(rows, out_dir, directions_path, limit, force, voice_mode, stale)
    if not todo:
        return 0
    directions = load_directions(directions_path)
    jobs, refs = build_jobs(todo, directions, voice_mode)
    tts_jobs = [j for j in jobs if j.original_path is None]
    orig_jobs = [j for j in jobs if j.original_path is not None]
    voice_files = build_voice_files(refs)
    segment_dir = out_dir / "segments"
    logger.info(f"🎙️ {len(todo)} lines: {len(tts_jobs)} Drama 3 segments, {len(orig_jobs)} original clips, "
                f"{len(voice_files)} voices ({voice_mode})")
    if tts_jobs:
        with Drama3Worker(threads=threads, device=device) as worker:
            for key, path in voice_files.items():
                worker.add_voice(key, path)
            for n, job in enumerate(sorted(tts_jobs, key=lambda j: (j.ref_key, j.row.strref, j.index)), 1):
                try:
                    speak(worker, job)
                except WorkerError as e:
                    logger.error(f"❌ {job.row.strref}#{job.index}: {e}")
                if job.take is not None:
                    job.take.save(segment_dir / f"{job.row.resref}_{job.index}.wav")
                if n % 10 == 0:
                    logger.info(f"   … {n}/{len(tts_jobs)} segments")
    return stitch_lines(todo, jobs, out_dir, directions_path, voice_mode, segment_dir)


# ==================== COMPARE PAGE ====================

def fish_dirs() -> List[Path]:
    """Every Drama 3 output folder (pst_revoice/audio_fish, audio_fish_<variant>)."""
    return sorted(p for p in OUT_DIR.parent.glob("audio_fish*") if p.is_dir())


def take_flag(segment: Dict) -> str:
    """" ⚠ <problem>" when a segment kept a flawed take, else ""."""
    attempts = segment.get("attempts") or []
    if not attempts:
        return ""
    last = attempts[-1]
    return "" if last == "ok" else f" ⚠ {html.escape(last)}"


def write_compare(plan: List[PlanRow]) -> None:
    """
    Write pst_revoice/compare.html: each line Drama 3 generated, with OmniVoice
    and every Drama 3 variant side by side and the exact text each variant sent.
    """
    variants = [(d.name.replace("audio_fish", "Drama 3").replace("_", " ").strip(), d,
                 json.loads((d / "generation.json").read_text(encoding="utf-8"))
                 if (d / "generation.json").exists() else {}) for d in fish_dirs()]
    strrefs = sorted({int(s) for _, _, gen in variants for s in gen})
    rows = {p.strref: p for p in plan}

    def audio(folder: Path, resref: str) -> str:
        path = folder / f"{resref}.WAV"
        if not path.exists():
            return "<small>(none)</small>"
        src = os.path.relpath(path, COMPARE_PATH.parent).replace("\\", "/")
        return f'<audio controls preload="none" src="{html.escape(src)}"></audio>'

    head = "".join(f"<th>{html.escape(label)}</th>" for label, _, _ in variants)
    body = []
    for strref in strrefs:
        row = rows.get(strref)
        if row is None:
            continue
        cells = []
        for _, folder, gen in variants:
            rec = gen.get(str(strref))
            sent_parts = []
            for s in (rec or {}).get("segments", []):
                extra = "" if s.get("source") == "original" else f", {s.get('seconds')}s, seed {s.get('seed')}"
                sent_parts.append(
                    f"<div class=seg><b>{html.escape(s.get('voice', ''))}</b> "
                    f"<code>{html.escape(s.get('resref') or s.get('text') or '')}</code>"
                    f"<small> {html.escape(s.get('source') or '')}{html.escape(extra)}"
                    f"{take_flag(s)}</small></div>")
            sent = "".join(sent_parts)
            cells.append(f"<td>{audio(folder, row.resref)}{sent}</td>")
        mood = next((g[str(strref)]["mood"] for _, _, g in variants if str(strref) in g), "-")
        body.append(f"<tr><td><b>{html.escape(row.speaker)}</b><br><small>{strref}</small><br>"
                    f"<span class=mood>{html.escape(mood)}</span></td><td class=line>{html.escape(row.text)}</td>"
                    f"<td>{audio(OMNI_DIR, row.resref)}</td>{''.join(cells)}</tr>")
    COMPARE_PATH.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>PST: OmniVoice vs Drama 3</title>
<style>
:root {{ --bg:#fff; --fg:#1d1d1f; --muted:#6b6b70; --line:#e3e3e6; --chip:#eef2ff; --chip-fg:#3730a3; --code:#f4f4f6; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141416; --fg:#ececf0; --muted:#9a9aa3; --line:#2c2c31;
  --chip:#262a4a; --chip-fg:#c7cbff; --code:#1f1f24; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.45 system-ui, sans-serif; margin:24px 16px; }}
table {{ border-collapse:collapse; width:100%; }}
th {{ text-align:left; padding:8px; border-bottom:2px solid var(--line); }}
td {{ border-bottom:1px solid var(--line); padding:10px 8px; vertical-align:top; }}
td.line {{ max-width:340px; }}
small {{ color:var(--muted); }}
code {{ background:var(--code); border-radius:4px; padding:1px 4px; font-size:12.5px; white-space:pre-wrap; }}
.seg {{ margin-top:6px; }}
.mood {{ background:var(--chip); color:var(--chip-fg); border-radius:6px; padding:1px 8px; font-weight:600; }}
audio {{ width:250px; max-width:100%; }}
</style></head><body><h1>PST voice lines: OmniVoice vs Fish Audio Drama 3</h1>
<p><small>Drama 3 = Fish Audio Drama 3 (Q4_K, speech.cpp). Built with Fish Audio. Each Drama 3 column shows the exact
text sent per segment: [direction], &lt;emphasis&gt; spans and [events].</small></p>
<table><tr><th>Character</th><th>Line</th><th>OmniVoice</th>{head}</tr>
{''.join(body)}
</table></body></html>
""", encoding="utf-8")
    logger.info(f"🎧 {COMPARE_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--speaker", action="append", help="Only this character (repeatable)")
    parser.add_argument("--strref", type=int, action="append", help="Only this strref (repeatable)")
    parser.add_argument("--strref-file", type=Path, help="Only the strrefs listed in this file (whitespace-separated)")
    parser.add_argument("--floats", action="store_true",
                        help="Only BCS idle barks (pst_revoice/floats.csv)")
    parser.add_argument("--limit", type=int, default=0, help="Stop after N new lines")
    parser.add_argument("--force", action="store_true", help="Regenerate existing audio")
    parser.add_argument("--stale", action="store_true",
                        help="Also regenerate WAVs whose snapshot no longer matches")
    parser.add_argument("--directions", type=Path, default=DIRECTIONS_PATH, help="Director output to use")
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="Output folder (audio_fish_<name> for variants)")
    parser.add_argument("--voice-mode", choices=["multi", "mood", "default"], default="multi",
                        help="One multi-clip voice per character (default), the line's mood clip, or the default clip")
    parser.add_argument("--threads", type=int, help="CPU threads for the experts the GPU can't hold")
    parser.add_argument("--device", help="Compute device for Drama 3 (e.g. Vulkan1, '4060 Ti', or from DRAMA3_DEVICE)")
    parser.add_argument("--no-player", action="store_true", help="Don't rewrite compare.html")
    parser.add_argument("--compare", action="store_true", help="Only rewrite compare.html")
    args = parser.parse_args()
    if args.strref_file:
        args.strref = list(args.strref or []) + [
            int(x) for x in args.strref_file.read_text(encoding="utf-8").split()]
        if not args.strref:
            logger.info("Strref file is empty; nothing to generate")
            return

    plan = [p for p in read_plan(PLAN_PATH) if p.action == "generate"]
    if not args.compare:
        rows = plan
        if args.speaker:
            rows = [p for p in rows if p.speaker in args.speaker]
        if args.floats:
            from pst_floats import is_float
            rows = [p for p in rows if is_float(p)]
        if args.strref:
            order = {s: i for i, s in enumerate(args.strref)}
            rows = sorted((p for p in rows if p.strref in order), key=lambda p: order[p.strref])
        args.out.mkdir(parents=True, exist_ok=True)
        generate(rows, args.out, args.directions, args.limit, args.force, args.threads, args.voice_mode,
                 args.stale, device=args.device)
    if not args.no_player:
        write_compare(plan)


if __name__ == "__main__":
    main()
