"""
PST Drama 3 pipeline status.

Counts and next-step text for pst_gui.py: what setup is still missing, what is
directed, what has audio, which WoW library voices still need transcripts, and
what to click next. overnight_steps() is the unattended Direct → Generate chain.
"""

import csv
import html
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import requests

from libs.appconfig import cfg
from libs.pst_references import load_library
from libs.tts_drama3 import DRAMA3_MODEL, SPEECH_EXE
from pst_classify import DEFAULT_LLM_URL
from pst_floats import EXPECTED_COUNT as FLOAT_TOTAL
from pst_floats import is_float
from pst_infopoints import EXPECTED_COUNT as EXAMINE_TOTAL
from pst_revoice import (
    CAST_NPCS_PATH,
    NARRATOR,
    ORIGINAL_VOICED,
    PlanRow,
    needs_direction,
    split_segments,
)

# ==================== CONFIGURATION ====================
STRATA_BASE = DEFAULT_LLM_URL.rsplit("/v1", 1)[0]
STRATA_MODEL = "qwen3.6-35b-a3b-ud-q4_k_m"
WHISPER_PYTHON = Path(os.environ.get("WHISPER_PYTHON", ".venv-omnivoice/Scripts/python.exe"))  # Whisper only
MOODS_JSON = Path("voices_moods_pst/moods.json")
MOOD_LABELS = Path("pst-mood-labels.json")
NWN_CATALOG = Path("voice_library/nwn/catalog.json")
WOW_CATALOG = Path("voice_library/wow/catalog.json")
CASTING_PATH = Path("pst_cast/casting.json")
OK_COLOR = "#166534"
BAD_COLOR = "#b91c1c"
# =======================================================


@dataclass
class PipelineStatus:
    """Counts the GUI shows at the top of the window."""

    lines: int
    directed: int
    need_direct: int
    generated: int
    need_audio: int
    wow_missing: int
    strata: str
    drama3: str
    examine: int = 0
    examine_audio: int = 0
    examine_stale: int = 0
    examine_total: int = 0
    floats: int = 0
    floats_audio: int = 0
    floats_stale: int = 0
    floats_total: int = 0
    dialog_report: bool = True
    cloned_voices: bool = True
    mood_refs: bool = True
    mood_labels: bool = True
    voice_library: bool = True
    casting: bool = True
    report_rows: int = 0
    cloned_count: int = 0
    cloned_needed: int = 0
    mood_characters: int = 0
    nwn_voices: int = 0
    wow_voices: int = 0
    wow_untranscribed: int = 0
    cast_dialogs: int = 0
    recast: int = 0                 # cast dialogs that break pst-cast-rules.json
    stale_direct: int = 0
    stale_audio: int = 0
    plan_stale: bool = False
    original_lines: int = 0
    original_mixed: int = 0


def has_speech(row: PlanRow, spoken: str) -> bool:
    """True when the line has at least one character-speech segment (not narrator-only)."""
    return any(voice != NARRATOR for voice, _ in split_segments(spoken, row.speaker))


@dataclass
class SpotCheckPick:
    """One unique conversation's sample line for a voice-audit listen."""

    dialog: str
    row: PlanRow
    speech_chars: int
    total_chars: int = 0


def load_unique_dialogs(npcs_path: Optional[Path] = None,
                        csv_path: Optional[Path] = None) -> Dict[str, List[int]]:
    """
    Conversation file (DLG resref) -> strrefs in that conversation.

    Prefers pst_cast/npcs.json (NPC SAY lines only). Falls back to the dialog
    report grouped by SystemName when the NPC list is missing.
    """
    npcs_path = npcs_path or CAST_NPCS_PATH
    if npcs_path.exists():
        try:
            npcs = json.loads(npcs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            npcs = []
        out: Dict[str, List[int]] = {}
        for npc in npcs:
            dlg = str(npc.get("dialog") or "").upper()
            if not dlg:
                continue
            refs = out.setdefault(dlg, [])
            seen = set(refs)
            for ln in npc.get("lines") or []:
                try:
                    strref = int(ln["strref"])
                except (KeyError, TypeError, ValueError):
                    continue
                if strref not in seen:
                    seen.add(strref)
                    refs.append(strref)
        if out:
            return out
    csv_path = Path(csv_path or getattr(cfg, "CSV_PATH", None) or "dialog-report.csv")
    if not csv_path.exists():
        return {}
    out = {}
    with csv_path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for row in csv.DictReader(handle):
            dlg = (row.get("SystemName") or "").strip().upper()
            if not dlg:
                continue
            try:
                strref = int(row["StrRef"])
            except (KeyError, TypeError, ValueError):
                continue
            refs = out.setdefault(dlg, [])
            if strref not in refs:
                refs.append(strref)
    return out


def _speech_extent(row: PlanRow, spoken: str) -> Tuple[int, int]:
    """(character-speech chars, all-segment chars) for scoring a sample line."""
    speech = total = 0
    for voice, text in split_segments(spoken, row.speaker):
        n = len(text)
        total += n
        if voice != NARRATOR:
            speech += n
    return speech, total


def spot_check_sample_stale(row: PlanRow, spoken: str, *, has_wav: bool,
                            generation: Optional[Dict], direction: Optional[Dict],
                            patcher_config: Optional[Dict] = None,
                            pronunciation: Optional[Dict] = None) -> List[str]:
    """
    Why an existing WAV does not count as a finished spot-check sample.

    Only recast (speaker or library voice changed) and stale direction reopen
    a conversation. Other audio drift is ignored so one current line still
    marks the whole conversation as sampled.
    """
    if not has_wav:
        return []
    reasons: List[str] = []
    rec = generation or {}
    if rec.get("speaker") and rec["speaker"] != row.speaker:
        reasons.append(f"speaker changed (was {rec['speaker']})")
    stored_voice = rec.get("voice")
    if stored_voice not in (None, "") and stored_voice != row.speech_voice:
        reasons.append(f"voice changed (was {stored_voice})")
    if reasons:
        return reasons
    if not direction:
        return []
    from pst_fish import direction_stale_reasons
    return direction_stale_reasons(
        row, spoken, patcher_config or {}, pronunciation or {}, direction)


def spot_check_picks(plan: Sequence[PlanRow], dialogs: Dict[str, Sequence[int]],
                     *, audio_dir: Path, directions: Optional[Dict[int, Dict]] = None,
                     overrides: Optional[Dict[int, str]] = None,
                     generation: Optional[Dict[int, Dict]] = None,
                     patcher_config: Optional[Dict] = None,
                     pronunciation: Optional[Dict] = None) -> List[SpotCheckPick]:
    """
    Unique conversations that still need a voice-audit sample, longest first.

    A conversation is skipped when any of its Drama 3-spoken lines already has
    a current WAV. It is eligible again if every voiced line is stale because
    the speaker/voice changed or the direction is stale. Examine lines and idle
    barks are not conversations. Originally kept game VO is not a sample.
    DTEST (the engine's dummy conversation) is never sampled.
    """
    directions = directions or {}
    overrides = overrides or {}
    generation = generation or {}
    by_ref = {p.strref: p for p in plan if p.action == "generate"}
    picks: List[SpotCheckPick] = []
    for dialog, strrefs in dialogs.items():
        if str(dialog).upper() == "DTEST":
            continue
        voiceable: List[Tuple[PlanRow, str, int, int]] = []
        sampled = False
        for strref in strrefs:
            row = by_ref.get(strref)
            if row is None or row.is_infopoint or is_float(row):
                continue
            spoken = overrides.get(row.strref, row.text)
            if not needs_direction(row, spoken):
                continue
            speech_chars, total_chars = _speech_extent(row, spoken)
            if speech_chars <= 0:
                continue
            has_wav = (audio_dir / f"{row.resref}.WAV").exists()
            if has_wav and not spot_check_sample_stale(
                    row, spoken, has_wav=True,
                    generation=generation.get(row.strref),
                    direction=directions.get(row.strref),
                    patcher_config=patcher_config,
                    pronunciation=pronunciation):
                sampled = True
                break
            voiceable.append((row, spoken, speech_chars, total_chars))
        if sampled or not voiceable:
            continue
        row, _spoken, speech_chars, total_chars = max(
            voiceable, key=lambda item: (item[2], item[3], -item[0].strref))
        picks.append(SpotCheckPick(dialog, row, speech_chars, total_chars))
    picks.sort(key=lambda p: (p.speech_chars, p.total_chars, -p.row.strref), reverse=True)
    return picks


def pick_spot_check(plan: Sequence[PlanRow], dialogs: Dict[str, Sequence[int]],
                    **kwargs) -> Optional[SpotCheckPick]:
    """The longest remaining voice-audit sample, or None when every conversation is sampled."""
    picks = spot_check_picks(plan, dialogs, **kwargs)
    return picks[0] if picks else None


def _json_nonempty(path: Path) -> bool:
    """True when path is JSON with at least one entry."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data)


def _csv_rows(path: Path) -> int:
    """Data rows in a CSV (header excluded). 0 if the file is missing."""
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        return max(0, sum(1 for _ in handle) - 1)


def voice_clip_names(path: Path) -> set:
    """Character names that have a clip in voices_pst/ (``Annah 1.WAV`` or a folder)."""
    if not path.is_dir():
        return set()
    names = set()
    for child in path.iterdir():
        if child.name.startswith(("_", ".")):
            continue
        if child.is_dir():
            names.add(child.name)
            continue
        if child.suffix.lower() not in (".wav", ".txt"):
            continue
        stem = child.stem
        if " " in stem and stem.split(" ")[-1].isdigit():
            stem = " ".join(stem.split(" ")[:-1])
        names.add(stem)
    return names


def original_cast_status(path: Path) -> Tuple[int, int, List[str]]:
    """
    How much of the original voiced PST cast is in voices_pst/.

    Returns:
        (have, needed, missing names). Unvoiced NPCs are not in the needed set.
    """
    have_names = voice_clip_names(path)
    missing = [name for name in ORIGINAL_VOICED if name not in have_names]
    return len(ORIGINAL_VOICED) - len(missing), len(ORIGINAL_VOICED), missing


def _mood_characters() -> int:
    if not MOODS_JSON.exists():
        return 0
    try:
        data = json.loads(MOODS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return len(data.get("characters") or {})


def _json_count(path: Path) -> int:
    """Number of keys or list items in a JSON file."""
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if isinstance(data, dict):
        return len(data)
    if isinstance(data, list):
        return len(data)
    return 0


def read_setup() -> Dict:
    """Which once-only inputs exist on disk, plus cheap counts for the checklist."""
    voices = Path(getattr(cfg, "VOICES_DIR", None) or "voices_pst")
    csv_path = Path(getattr(cfg, "CSV_PATH", None) or "dialog-report-pst-classic.csv")
    cloned_have, cloned_needed, _missing = original_cast_status(voices)
    report_rows = _csv_rows(csv_path)
    mood_characters = _mood_characters()
    cast_dialogs = _json_count(CASTING_PATH)
    try:
        from pst_cast import rule_breaks
        recast = len(rule_breaks())
    except Exception:       # missing / half-written casting files must not break the checklist
        recast = 0
    return {
        "dialog_report": csv_path.exists(),
        "cloned_voices": cloned_have == cloned_needed,
        "mood_refs": MOODS_JSON.exists(),
        "mood_labels": _json_nonempty(MOOD_LABELS),
        "voice_library": NWN_CATALOG.exists() and WOW_CATALOG.exists(),
        "casting": cast_dialogs > 0,
        "report_rows": report_rows,
        "cloned_count": cloned_have,
        "cloned_needed": cloned_needed,
        "mood_characters": mood_characters,
        "cast_dialogs": cast_dialogs,
        "recast": recast,
    }


def wow_voices_without_transcript(plan: Sequence[PlanRow],
                                  library: Optional[Dict[str, Dict]] = None) -> List[str]:
    """Cast WoW voice ids used in the plan whose reference clip has no transcript."""
    library = library if library is not None else load_library()
    used = {p.speech_voice for p in plan if p.action == "generate" and p.speech_voice.startswith("wow:")}
    return sorted(vid for vid in used if not (library.get(vid) or {}).get("reference_text"))


def collect_status(plan: Sequence[PlanRow], directions: Dict[int, Dict], overrides: Dict[int, str],
                   audio_dir: Path, library: Optional[Dict[str, Dict]] = None,
                   strata: Optional[str] = None, drama3: Optional[str] = None,
                   setup: Optional[Dict[str, bool]] = None,
                   generation: Optional[Dict[int, Dict]] = None,
                   moods: Optional[Dict[int, Dict]] = None,
                   patcher_config: Optional[Dict] = None,
                   pronunciation: Optional[Dict] = None,
                   plan_stale: bool = False) -> PipelineStatus:
    """
    How far the Drama 3 revoice has got.

    Args:
        plan: Planned generate rows.
        directions: pst_revoice/directions.json keyed by strref.
        overrides: Spoken-text edits keyed by strref.
        audio_dir: Folder of stitched Drama 3 WAVs (pst_revoice/audio_fish).
        library: Voice-library catalog; loaded from disk when omitted.
        strata: Precomputed Strata status (avoids a network call in tests).
        drama3: Precomputed Drama 3 install status.
        setup: Precomputed once-only file checks; read from disk when omitted.
        generation: audio_fish/generation.json keyed by strref; loaded from audio_dir
            when omitted.
        moods: Optional mood-cache rows (strref -> {Mood, ...}).
        patcher_config: When set with pronunciation, count stale directions and WAVs.
        pronunciation: Per-character pronunciation rules.
        plan_stale: True when dialog/casting inputs are newer than plan_meta.json.

    Returns:
        Counts plus Strata / Drama 3 readiness and setup flags.
    """
    library = library if library is not None else load_library()
    setup = setup if setup is not None else read_setup()
    moods = moods or {}
    if generation is None:
        gen_path = audio_dir / "generation.json"
        generation = {}
        if gen_path.exists():
            try:
                generation = {int(k): v for k, v in json.loads(gen_path.read_text(encoding="utf-8")).items()
                              if not str(k).startswith("_")}
            except (OSError, json.JSONDecodeError, ValueError):
                generation = {}
    nwn_voices = sum(1 for v in library.values() if str(v.get("id", "")).startswith("nwn:"))
    wow_voices = sum(1 for v in library.values() if str(v.get("id", "")).startswith("wow:"))
    wow_untranscribed = sum(1 for v in library.values()
                            if str(v.get("id", "")).startswith("wow:") and not v.get("reference_text"))
    generate_rows = [p for p in plan if p.action == "generate"]
    need_direct = 0
    stale_direct = 0
    stale_audio = 0
    generated = 0
    examine = 0
    examine_audio = 0
    examine_stale = 0
    floats = 0
    floats_audio = 0
    floats_stale = 0
    original_lines = 0
    original_mixed = 0
    directed = 0
    check_stale = patcher_config is not None and pronunciation is not None
    fish = None
    if check_stale:
        from pst_fish import audio_stale_reasons, direction_stale_reasons, line_mood
        fish = (audio_stale_reasons, direction_stale_reasons, line_mood)
    for p in generate_rows:
        has_wav = (audio_dir / f"{p.resref}.WAV").exists()
        if has_wav:
            generated += 1
        if p.speaker == NARRATOR:
            examine += 1
            if has_wav:
                examine_audio += 1
        elif is_float(p):
            floats += 1
            if has_wav:
                floats_audio += 1
        spoken = overrides.get(p.strref, p.text)
        if p.original:
            original_lines += 1
            segs = split_segments(spoken, p.speaker)
            if (any(voice != NARRATOR for voice, _ in segs)
                    and any(voice == NARRATOR for voice, _ in segs)):
                original_mixed += 1
        if needs_direction(p, spoken):
            if p.strref in directions:
                directed += 1
            else:
                need_direct += 1
            if check_stale and fish is not None and p.strref in directions:
                if fish[1](p, spoken, patcher_config, pronunciation, directions[p.strref]):
                    stale_direct += 1
        if has_wav and check_stale and fish is not None:
            mood = fish[2](p, directions, moods)
            if fish[0](p, spoken, patcher_config, pronunciation, directions, mood, generation.get(p.strref)):
                stale_audio += 1
                if p.speaker == NARRATOR:
                    examine_stale += 1
                elif is_float(p):
                    floats_stale += 1
    return PipelineStatus(
        lines=len(generate_rows),
        directed=directed,
        need_direct=need_direct,
        generated=generated,
        need_audio=len(generate_rows) - generated,
        wow_missing=len(wow_voices_without_transcript(generate_rows, library)),
        strata=strata if strata is not None else strata_status(),
        drama3=drama3 if drama3 is not None else drama3_status(),
        examine=examine,
        examine_audio=examine_audio,
        examine_stale=examine_stale,
        examine_total=EXAMINE_TOTAL,
        floats=floats,
        floats_audio=floats_audio,
        floats_stale=floats_stale,
        floats_total=FLOAT_TOTAL,
        dialog_report=setup.get("dialog_report", True),
        cloned_voices=setup.get("cloned_voices", True),
        mood_refs=setup.get("mood_refs", True),
        mood_labels=setup.get("mood_labels", True),
        voice_library=setup.get("voice_library", True),
        casting=setup.get("casting", True),
        report_rows=int(setup.get("report_rows") or 0),
        cloned_count=int(setup.get("cloned_count") or 0),
        cloned_needed=int(setup.get("cloned_needed") or 0),
        mood_characters=int(setup.get("mood_characters") or 0),
        nwn_voices=nwn_voices,
        wow_voices=wow_voices,
        wow_untranscribed=wow_untranscribed,
        cast_dialogs=int(setup.get("cast_dialogs") or 0),
        recast=int(setup.get("recast") or 0),
        stale_direct=stale_direct,
        stale_audio=stale_audio,
        plan_stale=plan_stale,
        original_lines=original_lines,
        original_mixed=original_mixed,
    )


def next_action(status: PipelineStatus) -> Tuple[str, str]:
    """
    What a new user should click now.

    Returns:
        (button key, instruction). Button key matches pst_gui.py highlight ids.
    """
    if not status.dialog_report:
        return ("dialog",
                "Next: Dialog report. If the game path is wrong, click Game config first and point it at classic PST.")
    if not status.cloned_voices:
        have = status.cloned_count
        need = status.cloned_needed or len(ORIGINAL_VOICED)
        return ("extract",
                f"Next: Extract cloned voices — {have} / {need} of the original voiced cast "
                f"(Morte, Annah, Dak'kon, …) are in voices_pst/.")
    if not status.mood_refs:
        if not status.mood_labels:
            return ("moods_collect",
                    "Next: Collect clone clips, then label deliveries in pst-mood-labels.json, then Build clone refs.")
        return ("moods_build", "Next: Build clone refs. Clips are labeled; this stitches them for Drama 3.")
    if not status.voice_library:
        return ("voicelib",
                "Next: Build voice library. Downloads Neverwinter Nights and WoW clips for everyone else.")
    if not status.casting:
        if status.strata != "loaded":
            return ("cast",
                    "Next: Cast NPCs — but start PolyStrata first (run-qwen36-ud-q4_k_m.bat), then click Cast NPCs.")
        return ("cast", "Next: Cast NPCs. Strata picks a library voice for each unvoiced dialog.")
    if status.lines == 0:
        return ("plan", "Next: Plan. That builds the list of every line that gets a new voice.")
    if status.plan_stale:
        return ("plan",
                f"Next: Plan — the dialog report or casting is newer than the plan ({status.lines:,} lines). "
                "Click Plan to rebuild; existing directions and audio stay until they look stale.")
    if status.recast:
        start = "" if status.strata == "loaded" else "start PolyStrata first (run-qwen36-ud-q4_k_m.bat), then "
        return ("cast", f"Next: Cast NPCs — {status.recast:,} dialogs use a voice pst-cast-rules.json no longer "
                        f"allows (child, Deekin, spirit, WoW-race rules); {start}click Cast NPCs to re-pick them, then Plan.")
    if status.need_direct or status.stale_direct:
        if status.need_direct and status.stale_direct:
            detail = (f"{status.need_direct:,} of {status.lines:,} lines have no [direction] yet, "
                      f"{status.stale_direct:,} are stale")
        elif status.need_direct:
            detail = f"{status.need_direct:,} of {status.lines:,} lines have no [direction] yet"
        else:
            detail = (f"{status.stale_direct:,} directed lines no longer match spoken text, speaker, "
                      "voice, or pronunciation")
        if status.strata != "loaded":
            return ("direct",
                    f"Next: Direct missing — {detail}. "
                    "Start PolyStrata first, then click Direct missing (overnight is fine).")
        return ("direct",
                f"Next: Direct missing — {detail}. "
                "Click Direct missing (overnight is fine; up-to-date lines are skipped).")
    if status.wow_missing:
        return ("transcribe",
                f"Next: Transcribe WoW — {status.wow_missing:,} WoW voices used in the plan have no transcript, "
                "so Drama 3 will skip those NPCs.")
    if status.drama3 != "ready" and (status.need_audio or status.stale_audio):
        return ("drama3", f"Next: install Drama 3 ({status.drama3}), then click Generate missing.")
    if status.need_audio or status.stale_audio:
        examine_left = status.examine - status.examine_audio
        if status.examine and examine_left == status.need_audio and not status.stale_audio:
            return ("generate_examine",
                    f"Next: Generate examine — {examine_left:,} Narrator ?-click lines still need audio.")
        float_left = status.floats - status.floats_audio
        if status.floats and float_left == status.need_audio and not status.stale_audio:
            return ("generate_floats",
                    f"Next: Generate idle barks — {float_left:,} BCS overhead lines still need audio.")
        bits = []
        if status.need_audio:
            bits.append(f"{status.need_audio:,} of {status.lines:,} lines have no WAV yet")
        if status.stale_audio:
            bits.append(f"{status.stale_audio:,} existing WAVs are stale")
        return ("generate",
                f"Next: Generate missing — {'; '.join(bits)}. "
                "Can run for days; up-to-date files are skipped, stale ones are regenerated. "
                "Or click Run overnight to Direct, free the GPU, and Generate without watching.")
    return ("build",
            "Next: listen in the list (regenerate anything you dislike), then Build mod when you are happy.")


def next_step(status: PipelineStatus) -> str:
    """One sentence for the GUI: what to do now."""
    return next_action(status)[1]


def status_line(status: PipelineStatus) -> str:
    """Plain-text work counts (tests and logs)."""
    return " · ".join(text for _ok, text in work_items(status))


def setup_line(status: PipelineStatus) -> str:
    """Plain-text setup checklist (tests and logs)."""
    return "Setup: " + " · ".join(("✓ " if ok else "✗ ") + text for ok, text in setup_items(status))


def setup_items(status: PipelineStatus) -> List[Tuple[bool, str]]:
    """(done, label with numbers) for each once-only setup step."""
    dialog = (f"Dialog report: {status.report_rows:,} lines" if status.dialog_report
              else "Dialog report: missing — click Dialog report")
    need = status.cloned_needed or len(ORIGINAL_VOICED)
    if status.cloned_voices:
        cloned = (f"Cloned voices: {status.cloned_count} / {need} original cast"
                  if status.cloned_needed else "Cloned voices: original cast present")
    elif status.cloned_count:
        cloned = (f"Cloned voices: {status.cloned_count} / {need} original cast "
                  "— click Extract cloned voices")
    else:
        cloned = "Cloned voices: missing — click Extract cloned voices"
    moods = (f"Clone refs: {status.mood_characters} characters" if status.mood_refs
             else "Clone refs: missing — Collect clone clips, then Build clone refs")
    if status.voice_library:
        wow_txt = (f"{status.wow_voices} WoW ({status.wow_untranscribed} without text)"
                   if status.wow_untranscribed else f"{status.wow_voices} WoW")
        library = f"Voice library: {status.nwn_voices} NWN + {wow_txt}"
    else:
        library = "Voice library: missing — click Build voice library"
    casting = (f"Casting: {status.cast_dialogs:,} dialogs" if status.casting
               else "Casting: missing — click Cast NPCs")
    if status.casting and status.recast:
        casting += f" ({status.recast:,} break the cast rules — Cast NPCs)"
    if not status.lines:
        plan = "Plan: none — click Plan"
        plan_ok = False
    elif status.plan_stale:
        plan = f"Plan: {status.lines:,} lines (inputs changed — click Plan)"
        plan_ok = False
    else:
        plan = f"Plan: {status.lines:,} lines"
        plan_ok = True
    drama = ("Drama 3: ready" if status.drama3 == "ready" else f"Drama 3: {status.drama3}")
    return [
        (status.dialog_report, dialog),
        (status.cloned_voices, cloned),
        (status.mood_refs, moods),
        (status.voice_library, library),
        (status.casting and not status.recast, casting),
        (plan_ok, plan),
        (status.drama3 == "ready", drama),
    ]


def work_items(status: PipelineStatus) -> List[Tuple[bool, str]]:
    """(done, label with numbers) for directing / audio / WoW / examine."""
    if status.lines == 0:
        return [(False, "No plan yet — click Plan")]
    direct_extra = []
    if status.need_direct:
        direct_extra.append(f"{status.need_direct:,} left")
    if status.stale_direct:
        direct_extra.append(f"{status.stale_direct:,} stale")
    audio_extra = []
    if status.need_audio:
        audio_extra.append(f"{status.need_audio:,} left")
    if status.stale_audio:
        audio_extra.append(f"{status.stale_audio:,} stale")
    items = [
        (status.need_direct == 0 and status.stale_direct == 0,
         f"Directed {status.directed:,} / {status.lines:,}"
         + (f" ({', '.join(direct_extra)})" if direct_extra else "")),
        (status.need_audio == 0 and status.stale_audio == 0,
         f"Audio {status.generated:,} / {status.lines:,}"
         + (f" ({', '.join(audio_extra)})" if audio_extra else "")),
    ]
    if status.original_lines:
        mix = f", {status.original_mixed:,} with narrator to stitch" if status.original_mixed else ""
        items.append((True, f"Original VO {status.original_lines:,} lines kept{mix}"))
    if status.wow_voices or status.wow_missing:
        items.append((status.wow_missing == 0,
                      f"WoW in plan: {status.wow_missing:,} still need transcripts"
                      if status.wow_missing else "WoW in plan: transcripts filled"))
    items.append(_kind_item(
        "Examine", status.examine, status.examine_audio, status.examine_stale,
        status.examine_total or EXAMINE_TOTAL))
    items.append(_kind_item(
        "Idle barks", status.floats, status.floats_audio, status.floats_stale,
        status.floats_total or FLOAT_TOTAL))
    return items


def _kind_item(label: str, in_plan: int, audio: int, stale: int, total: int) -> Tuple[bool, str]:
    """Checklist chip for examine / idle-bark import + audio."""
    audio_left = max(0, in_plan - audio)
    import_left = max(0, total - in_plan)
    extras = []
    if import_left:
        extras.append(f"{import_left:,} not imported")
    if audio_left:
        extras.append(f"{audio_left:,} no WAV")
    if stale:
        extras.append(f"{stale:,} stale")
    text = f"{label} {in_plan:,} / {total:,} in plan"
    if in_plan:
        text += f" · audio {audio:,} / {in_plan:,}"
    if extras:
        text += f" ({', '.join(extras)})"
    return (import_left == 0 and audio_left == 0 and stale == 0, text)


def _chips_html(items: List[Tuple[bool, str]]) -> str:
    """Colored HTML chips: green done, red still missing."""
    parts = []
    for ok, text in items:
        color = OK_COLOR if ok else BAD_COLOR
        weight = "400" if ok else "700"
        parts.append(f'<span style="color:{color};font-weight:{weight}">{html.escape(text)}</span>')
    return " &nbsp;·&nbsp; ".join(parts)


def setup_html(status: PipelineStatus) -> str:
    """Checklist HTML for the GUI (red = missing)."""
    return "Setup: " + _chips_html(setup_items(status))


def work_html(status: PipelineStatus) -> str:
    """Work-count HTML for the GUI (red = still to do)."""
    return "Work: " + _chips_html(work_items(status))


def strata_status() -> str:
    """PolyStrata at port 8080: loaded, server up, or not running."""
    try:
        response = requests.get(f"{STRATA_BASE}/health", timeout=2)
        if not response.ok:
            return "not running"
        try:
            data = response.json()
        except ValueError:
            data = {}
        if data.get("loaded") is False:
            return "server up"
        text = response.text.lower()
        if STRATA_MODEL.lower() in text or data.get("loaded") is True:
            return "loaded"
        return "server up"
    except requests.RequestException:
        return "not running"


def drama3_status() -> str:
    """speech.exe + Q4_K weights on disk, or what is missing."""
    if not SPEECH_EXE.exists():
        return f"speech.exe missing ({SPEECH_EXE})"
    if not DRAMA3_MODEL.exists():
        return f"model missing ({DRAMA3_MODEL})"
    return "ready"


def unload_strata() -> None:
    """Free the GPU for Drama 3; the PolyStrata server keeps running."""
    try:
        requests.post(f"{STRATA_BASE}/unload", json={}, timeout=60)
    except requests.RequestException:
        pass


def load_strata(timeout: int = 600) -> None:
    """Load the Qwen3.6 model into a running PolyStrata server (blocks until ready)."""
    requests.post(f"{STRATA_BASE}/load", json={}, timeout=timeout)


def strata_root() -> Path:
    """PolyStrata checkout with serve/server.py (POLYSTRATA_ROOT overrides)."""
    env = os.environ.get("POLYSTRATA_ROOT")
    candidates = []
    if env:
        candidates.append(Path(env))
    home = Path.home()
    candidates += [
        home / "Documents" / "GitHub" / "PolyStrata-main",
        home / "PolyStrata-main",
        Path(r"D:\PolyStrata-main"),
        Path(r"C:\PolyStrata-main"),
    ]
    for path in candidates:
        if (path / "serve" / "server.py").is_file():
            return path
    return candidates[0]


def strata_server_cmd() -> Optional[List[str]]:
    """Argv to start PolyStrata on port 8080, or None if the checkout is missing."""
    root = strata_root()
    python = root / ".venv" / "Scripts" / "python.exe"
    server = root / "serve" / "server.py"
    config = root / "strata-qwen36-ud-q4_k_m.json"
    if not python.is_file() or not server.is_file() or not config.is_file():
        return None
    return [str(python), str(server), "--engine", "strata", "--config", str(config), "--port", "8080"]


def start_strata_server() -> bool:
    """Start PolyStrata in a new console if it is not already listening. False if missing."""
    if strata_status() != "not running":
        return True
    cmd = strata_server_cmd()
    if not cmd:
        return False
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_CONSOLE
    subprocess.Popen(cmd, cwd=str(strata_root()), creationflags=flags)
    return True


@dataclass
class OvernightStep:
    """One unattended pipeline step. want_strata True=model loaded, False=unloaded."""

    key: str
    label: str
    want_strata: Optional[bool] = None


def overnight_blocker(status: PipelineStatus) -> Optional[str]:
    """Why Run overnight cannot start, or None if the long jobs can proceed."""
    if not status.dialog_report:
        return "Dialog report is missing. Click Dialog report first."
    if not status.cloned_voices:
        return "Cloned voices are incomplete. Click Extract cloned voices first."
    if not status.mood_refs:
        return "Clone refs are missing. Collect clone clips, label them, then Build clone refs."
    if not status.voice_library:
        return "Voice library is missing. Click Build voice library first."
    return None


def overnight_steps(status: PipelineStatus) -> List[OvernightStep]:
    """
    Remaining unattended work, in GPU-safe order: imports, Strata jobs, unload, generate.

    Recompute after every step — Direct/Generate skip finished work, so this resumes.
    """
    steps: List[OvernightStep] = []
    if not status.lines or status.plan_stale:
        steps.append(OvernightStep("plan", "Writing the revoice plan…"))
    examine_import = max(0, (status.examine_total or EXAMINE_TOTAL) - status.examine)
    if examine_import:
        steps.append(OvernightStep("import_examine",
                                   f"Importing {examine_import:,} Narrator examine lines…"))
    float_import = max(0, (status.floats_total or FLOAT_TOTAL) - status.floats)
    if float_import:
        steps.append(OvernightStep("import_floats",
                                   f"Importing {float_import:,} idle barks…"))
    if not status.casting:      # rule-break recasts are a click on Cast NPCs (a failed pick would loop here)
        steps.append(OvernightStep("ensure_strata", "Starting PolyStrata…", want_strata=True))
        steps.append(OvernightStep("cast", "Casting NPCs (resumable)…", want_strata=True))
    if status.need_direct or status.stale_direct:
        steps.append(OvernightStep("ensure_strata", "Loading Strata for directing…", want_strata=True))
        detail = []
        if status.need_direct:
            detail.append(f"{status.need_direct:,} missing")
        if status.stale_direct:
            detail.append(f"{status.stale_direct:,} stale")
        steps.append(OvernightStep(
            "direct",
            f"Directing {', '.join(detail) or 'lines'} (overnight is fine)…",
            want_strata=True))
    if status.wow_missing:
        steps.append(OvernightStep("free_gpu", "Unloading Strata so Whisper can use the GPU…",
                                   want_strata=False))
        steps.append(OvernightStep(
            "transcribe",
            f"Transcribing {status.wow_missing:,} WoW voices…",
            want_strata=False))
    need_wav = status.need_audio or status.stale_audio
    if not need_wav:
        examine_left = max(0, status.examine - status.examine_audio)
        float_left = max(0, status.floats - status.floats_audio)
        need_wav = bool(examine_left or float_left or status.examine_stale or status.floats_stale)
    if need_wav:
        steps.append(OvernightStep("free_gpu", "Unloading Strata so Drama 3 can use the GPU…",
                                   want_strata=False))
        bits = []
        if status.need_audio:
            bits.append(f"{status.need_audio:,} missing")
        if status.stale_audio:
            bits.append(f"{status.stale_audio:,} stale")
        steps.append(OvernightStep(
            "generate",
            f"Generating {', '.join(bits) or 'missing'} lines with Drama 3 (can run for days)…",
            want_strata=False))
    return steps


OVERNIGHT_PATH = Path("pst_revoice/overnight.json")


def load_overnight_state(path: Path = OVERNIGHT_PATH) -> Dict:
    """overnight.json, or {} if missing."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_overnight_state(state: Dict, path: Path = OVERNIGHT_PATH) -> None:
    """Persist overnight chain status so a later click can resume."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def audio_is_stale(record: Optional[Dict], current_texts: List[str]) -> bool:
    """True when existing Drama 3 audio no longer matches the directed segment texts."""
    if not record:
        return True
    return [s.get("text", "") for s in record.get("segments", [])] != current_texts
