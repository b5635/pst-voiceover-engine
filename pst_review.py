#!/usr/bin/env python3
"""
PST Revoice review queue
Flags planned lines a person should listen to or fix by hand, and writes a
"bird's-eye" conversation page per DLG to play a whole conversation.

Flags (severity "review" puts a line in the GUI's Needs review filter until
you mark it reviewed; "info" is shown but does not queue it):

    quotes        review  doubled ("") or unbalanced quote marks in the game text
    read-out      info    a quoted word / number / inscription is read by the narrator
    unquoted      review  narration holds what looks like unquoted ALL-CAPS speech
    narr-1st      review  narration written in the first person (speech without quotes?)
    speech-3rd    review  a quoted part reads like narration ("He suddenly begins ...")
    emote         info    a ::emote:: became a sound or was dropped
    take          review  Drama 3 kept a flawed take (cut off / stalled / too short)
    pace          review  the WAV is very short or long for its words (dropped words? long stall?)
    toned         info    the delivery guard toned the director's volume / whisper words down
    words         info    the director changed words, only its [direction] is used
    stale         info    the WAV no longer matches the line (Generate missing redoes it)
    note          review  you left a note on the line
    player-quote  review  the Nameless One's own words ('"Hi," you say') are spoken in the NPC's voice
    dev-text      review  developer / placeholder text left in the game (AR0200, "test character", ...)

    python pst_review.py                    write pst_revoice/review.csv and print a summary
    python pst_review.py --tone-pass        apply the delivery guard to directions.json (Strata entries)
    python pst_review.py --conversation DPHAROD   write pst_revoice/conversations/DPHAROD.html

Reviewed marks live in pst-line-review.json: {strref: {"codes": [...], "at": ...}}.
A line comes back into the queue when it gets a review flag it did not have
when you marked it.
"""

import argparse
import csv
import html
import json
import re
import sys
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from libs.appconfig import cfg
from libs.dlg_tree import _parse
from libs.utils import iter_files_ci, load_patcher_config
from pst_classify import PLAYER
from pst_fish import (
    PATCHER_CONFIG_PATH,
    audio_stale_reasons,
    load_directions,
    save_directions,
    spoken_words,
)
from pst_revoice import (
    FISH_AUDIO_DIR,
    NARRATOR,
    PLAN_PATH,
    WORK_DIR,
    PlanRow,
    dialog_of_strrefs,
    fix_doubled_quotes,
    load_generation,
    load_line_notes,
    load_line_overrides,
    load_pronunciation,
    read_plan,
    split_segments,
)

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# ==================== CONFIGURATION ====================
REVIEW_PATH = Path("pst-line-review.json")
REVIEW_CSV = WORK_DIR / "review.csv"
CONVERSATION_DIR = WORK_DIR / "conversations"
MIN_SECONDS_PER_WORD = 0.2      # a WAV faster than this probably dropped words
MAX_SECONDS_PER_WORD = 1.3      # slower than this probably stalls
PACE_MIN_WORDS = 5
CAPS_RUN_RE = re.compile(r"(?:\b[A-Z][A-Z'\-]+\b[\s.!?,;:\-]*){4,}")
FIRST_PERSON_RE = re.compile(r"\b(I|I'm|I'll|I've|I'd)\b|\b(me|my|mine|myself)\b")
THIRD_PERSON_START_RE = re.compile(
    r"^(?:(?:He|She|It|They|The (?:man|woman|old man|old woman|little man|corpse|skeleton|spirit|creature|figure))"
    r"\s+(?:\w+ly\s+)?(?:nods|smiles|sighs|looks at|glances|turns|shrugs|begins|stops|frowns|grins|shakes|stares|"
    r"pauses|gives|hands|points|leans|steps|falls|raises|lowers|rubs|scratches|regards|considers|retches|walks|"
    r"moves|reaches|watches|coughs|spits|waves|gestures|blinks|tilts|cocks|bows|kneels|sits|squints|grimaces|"
    r"winces|flinches|chuckles|laughs)\b"
    r"|You\s+(?:\w+ly\s+)?(?:hop|step back|notice|feel|realize|hear|watch|see that)\b)")
PLAYER_REPLY_RE = re.compile(r"^you\s+(?:\w+ly\s+)?(say|said|mutter|reply|ask|whisper|answer|respond|tell|add)\w*\b",
                             re.IGNORECASE)
DEV_TEXT_RE = re.compile(r"\bAR\d{4}\b|\bthe player (?:can|will|should|may)\b|\btest character\b|\bplaceholder\b"
                         r"|\bI am a\s+\"?generic|\bNPC\b|\bTODO\b", re.IGNORECASE)
SINGLE_QUOTED_RE =re.compile(r"(?<![A-Za-z])'[^']{2,}'(?![A-Za-z])")
SEVERITY = {"quotes": "review", "read-out": "info", "unquoted": "review", "narr-1st": "review",
            "speech-3rd": "review", "emote": "info", "take": "review", "pace": "review", "toned": "info",
            "words": "info", "stale": "info", "note": "review", "player-quote": "review", "dev-text": "review"}
PLAYER_LABEL = PLAYER            # how DialogContext names the player's replies
# =======================================================


@dataclass
class Flag:
    code: str
    text: str

    @property
    def severity(self) -> str:
        return SEVERITY.get(self.code, "review")

    def __str__(self) -> str:
        return f"{'⚑' if self.severity == 'review' else '·'} {self.code}: {self.text}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ==================== TEXT CHECKS ====================

def text_flags(text: str, speaker: str) -> List[Flag]:
    """Flags from the line text and how split_segments() divides it."""
    flags: List[Flag] = []
    if re.search(r'"{2,}', text):
        flags.append(Flag("quotes", 'doubled quote marks ("") — read as one; check who says what'))
    if fix_doubled_quotes(text).count('"') % 2:
        flags.append(Flag("quotes", "odd number of quote marks — the last quote runs to the end of the line"))
    segments = split_segments(text, speaker)
    for voice, seg in segments:
        if voice == NARRATOR:
            quoted = re.findall(r'"([^"]+)"', seg)
            if quoted:
                shown = ", ".join(f'"{q.strip()[:30]}"' for q in quoted[:3])
                flags.append(Flag("read-out", f"narrator reads {shown}"))
            outside = re.sub(r'"[^"]*"', " ", seg)
            caps = CAPS_RUN_RE.search(outside)
            if caps and not quoted:
                flags.append(Flag("unquoted", f"ALL-CAPS run in narration: {caps.group(0).strip()[:50]}"))
            plain = SINGLE_QUOTED_RE.sub(" ", outside)
            if len(FIRST_PERSON_RE.findall(plain)) >= 2 and not plain.lstrip().startswith(("*(", "(", "NOTE")):
                flags.append(Flag("narr-1st", f"first-person narration: {outside.strip()[:60]}"))
        elif THIRD_PERSON_START_RE.match(seg):
            flags.append(Flag("speech-3rd", f"speech reads like narration: {seg[:60]}"))
    for (v1, _), (v2, narr) in zip(segments, segments[1:]):
        if v1 != NARRATOR and v2 == NARRATOR and PLAYER_REPLY_RE.match(narr):
            flags.append(Flag("player-quote", f"the quote before '{narr[:30]}' is the player's words, "
                                              "spoken in the NPC voice — edit the spoken text"))
    dev = DEV_TEXT_RE.search(text)
    if dev:
        flags.append(Flag("dev-text", f"developer / placeholder text ({dev.group(0)}) — skip or rewrite it"))
    emotes = re.findall(r"::[^:]+::", text)
    if emotes:
        flags.append(Flag("emote", f"{', '.join(emotes[:3])} spoken as a sound (or dropped if silent)"))
    return flags


# ==================== AUDIO CHECKS ====================

def wav_seconds(path: Path) -> Optional[float]:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (OSError, wave.Error, EOFError):
        return None


def audio_flags(record: Optional[Dict], seconds: Optional[float]) -> List[Flag]:
    """Flags from generation.json (kept takes) and the stitched WAV's length."""
    if not record:
        return []
    flags: List[Flag] = []
    words = 0
    for i, seg in enumerate(record.get("segments") or []):
        attempts = seg.get("attempts") or []
        if attempts and attempts[-1] != "ok":
            flags.append(Flag("take", f"segment {i + 1} kept a flawed take: {attempts[-1]} "
                                      f"({len(attempts)} tries)"))
        if seg.get("source") != "original":
            words += len(spoken_words(seg.get("text", "")))
    if seconds and words >= PACE_MIN_WORDS:
        rate = seconds / words
        if rate < MIN_SECONDS_PER_WORD:
            flags.append(Flag("pace", f"{seconds:.1f}s for {words} words — words may be missing"))
        elif rate > MAX_SECONDS_PER_WORD:
            flags.append(Flag("pace", f"{seconds:.1f}s for {words} words — long pauses or a stall"))
    return flags


# ==================== AUDIT ====================

def audit(plan: Sequence[PlanRow], overrides: Dict[int, str], directions: Dict[int, Dict],
          generation: Dict[int, Dict], notes: Dict[int, str], audio_dir: Path = FISH_AUDIO_DIR,
          patcher_config: Optional[Dict] = None, pronunciation: Optional[Dict] = None,
          moods: Optional[Dict[int, Dict]] = None) -> Dict[int, List[Flag]]:
    """strref -> flags for every planned line that has any."""
    patcher_config = patcher_config if patcher_config is not None else load_patcher_config(PATCHER_CONFIG_PATH)
    pronunciation = pronunciation if pronunciation is not None else load_pronunciation()
    moods = moods or {}
    result: Dict[int, List[Flag]] = {}
    for row in plan:
        spoken = overrides.get(row.strref, row.text)
        flags = text_flags(spoken, row.speaker)
        entry = directions.get(row.strref) or {}
        if entry.get("toned"):
            flags.append(Flag("toned", "; ".join(dict.fromkeys(entry["toned"]))[:160]))
        if entry and entry.get("valid") is False:
            flags.append(Flag("words", "the director changed words; only its [direction] is used"))
        wav = audio_dir / f"{row.resref}.WAV"
        record = generation.get(row.strref)
        if wav.exists():
            flags += audio_flags(record, wav_seconds(wav))
            mood = (moods.get(row.strref) or {}).get("Mood") or entry.get("mood", "")
            stale = audio_stale_reasons(row, spoken, patcher_config, pronunciation, directions, mood, record)
            if stale:
                flags.append(Flag("stale", "; ".join(stale)[:160]))
        if notes.get(row.strref):
            flags.append(Flag("note", notes[row.strref][:160]))
        if flags:
            result[row.strref] = flags
    return result


def review_codes(flags: Iterable[Flag]) -> List[str]:
    return sorted({f.code for f in flags if f.severity == "review"})


def load_reviewed(path: Path = REVIEW_PATH) -> Dict[int, Dict]:
    if not path.exists():
        return {}
    return {int(k): v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if not k.startswith("_")}


def save_reviewed(reviewed: Dict[int, Dict], path: Path = REVIEW_PATH) -> None:
    data = {"_comment": "Lines marked reviewed in PST Revoice: the review flags they had then. "
                        "A new review flag puts the line back in the queue."}
    data.update({str(k): v for k, v in sorted(reviewed.items())})
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def mark_reviewed(reviewed: Dict[int, Dict], strref: int, flags: Iterable[Flag]) -> None:
    reviewed[strref] = {"codes": review_codes(flags), "at": _now()}


def needs_review(flags: Iterable[Flag], mark: Optional[Dict]) -> bool:
    """True when the line has a review flag it did not have when it was marked reviewed."""
    codes = review_codes(flags)
    if not codes:
        return False
    return not mark or bool(set(codes) - set(mark.get("codes") or []))


def write_review_csv(plan: Sequence[PlanRow], flags: Dict[int, List[Flag]], reviewed: Dict[int, Dict],
                     dialog_of: Dict[int, str], path: Path = REVIEW_CSV) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["StrRef", "Speaker", "Dialog", "NeedsReview", "Codes", "Flags", "Text"])
        for row in plan:
            fl = flags.get(row.strref)
            if not fl:
                continue
            w.writerow([row.strref, row.speaker, dialog_of.get(row.strref, ""),
                        "yes" if needs_review(fl, reviewed.get(row.strref)) else "",
                        " ".join(sorted({x.code for x in fl})), " | ".join(x.text for x in fl),
                        row.text.replace("\n", " ")[:300]])


# ==================== TONE PASS ====================

def tone_pass(plan: Sequence[PlanRow], directions: Dict[int, Dict], overrides: Dict[int, str]) -> Tuple[int, int]:
    """
    Apply libs.pst_tone to every Strata direction (manual ones are left alone).
    Returns (entries changed, entries checked). The director's own text is kept in each segment's "raw".
    """
    from pst_direct import githzerai_speakers, is_githzerai, toned_segments
    gith = githzerai_speakers()
    dialog_of = dialog_of_strrefs()
    rows = {p.strref: p for p in plan}
    changed = checked = 0
    for strref, entry in directions.items():
        row = rows.get(strref)
        if not row or not str(entry.get("source", "")).startswith("strata"):
            continue
        checked += 1
        line = overrides.get(strref, row.text)
        segments, changes = toned_segments(entry.get("segments") or [], line, is_githzerai(row, gith, dialog_of))
        if segments != entry.get("segments"):
            entry["segments"] = segments
            changed += 1
        if changes:
            entry["toned"] = changes
        else:
            entry.pop("toned", None)
    return changed, checked


# ==================== CONVERSATION PAGE ====================

def dlg_path(dlg: str, folder: Optional[Path] = None) -> Optional[Path]:
    folder = folder or Path(cfg.EXTRACT_DIR)
    return next((p for p in iter_files_ci(folder, "dlg") if p.stem.upper() == dlg.upper()), None)


def load_texts(csv_path: Optional[Path] = None) -> Dict[int, Tuple[str, str]]:
    """strref -> (speaker, text) from the dialog report (NPC lines and player replies)."""
    with open(csv_path or cfg.CSV_PATH, "r", encoding="utf-8-sig") as f:
        return {int(r["StrRef"]): (r["RealName"], r["Text"]) for r in csv.DictReader(f)}


def _audio_tag(src: Path) -> str:
    return f'<audio controls preload="none" src="{html.escape(src.as_posix())}"></audio>'


def conversation_html(dlg: str, texts: Dict[int, Tuple[str, str]], plan: Sequence[PlanRow],
                      directions: Dict[int, Dict], flags: Dict[int, List[Flag]],
                      audio_dir: Path = FISH_AUDIO_DIR, out_dir: Path = CONVERSATION_DIR,
                      focus: Optional[int] = None) -> Optional[Path]:
    """
    Write one DLG as a page: every NPC line (state) with its audio, directions
    and flags, and the player's replies as links to the line they lead to.
    Clicking a reply jumps there and plays it; "Auto-continue" follows lines
    that have a single reply.
    """
    path = dlg_path(dlg)
    parsed = _parse(path) if path else None
    if not parsed:
        return None
    says, transitions = parsed
    rows = {p.strref: p for p in plan}
    out_dir.mkdir(parents=True, exist_ok=True)
    rel_audio = Path("..") / audio_dir.name
    original_rel = Path("..") / "original"
    replies: Dict[int, List[Tuple[int, str, int]]] = {}
    for src, tflags, text, next_dlg, next_state in transitions:
        replies.setdefault(src, []).append((text if tflags & 1 else -1, next_dlg, next_state))
    esc = html.escape
    cards = []
    for i, strref in enumerate(says):
        speaker, text = texts.get(strref, ("", ""))
        row = rows.get(strref)
        audio = ""
        if row and (audio_dir / f"{row.resref}.WAV").exists():
            audio = _audio_tag(rel_audio / f"{row.resref}.WAV")
        elif row and row.original and (WORK_DIR / "original" / f"{row.original}.wav").exists():
            audio = _audio_tag(original_rel / f"{row.original}.wav") + " <small>original</small>"
        elif row:
            audio = "<small>not generated</small>"
        dirs = "".join(f"<div class=dir>{esc(s.get('text', ''))}</div>"
                       for s in (directions.get(strref) or {}).get("segments", []))
        flag_html = "".join(f"<div class='flag {f.severity}'>{esc(str(f))}</div>" for f in flags.get(strref, []))
        reps = []
        for rtext, ndlg, nstate in replies.get(i, []):
            label = esc(texts.get(rtext, ("", "(no text)"))[1]) if rtext >= 0 else "<i>(continue)</i>"
            if not ndlg:
                reps.append(f"<li>{label} <small>— ends</small></li>")
            elif ndlg == dlg.upper():
                reps.append(f"<li><a href='#s{nstate}' data-go='s{nstate}'>{label}</a></li>")
            else:
                reps.append(f"<li><a href='{esc(ndlg)}.html#s{nstate}'>{label}</a> "
                            f"<small>→ {esc(ndlg)}</small></li>")
        cls = " focus" if focus == strref else ""
        cards.append(
            f"<section id='s{i}' class='card{cls}' data-strref='{strref}'><header><b>{esc(speaker or dlg)}</b> "
            f"<small>state {i} · strref {strref}</small></header><p>{esc(text)}</p>{audio}{dirs}{flag_html}"
            f"<ul>{''.join(reps)}</ul></section>")
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(dlg)} conversation</title>
<style>
:root {{ --bg:#fff; --fg:#1d1d1f; --muted:#6b6b70; --line:#e3e3e6; --card:#fafafa; --dir:#1d4ed8;
  --flag:#b45309; --hl:#fef9c3; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141416; --fg:#ececf0; --muted:#9a9aa3; --line:#2c2c31;
  --card:#1c1c20; --dir:#93c5fd; --flag:#fbbf24; --hl:#3f3a12; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.45 system-ui, sans-serif; margin:16px; max-width:980px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px 14px; margin:10px 0; }}
.card.focus, .card:target {{ background:var(--hl); }}
small {{ color:var(--muted); }} .dir {{ color:var(--dir); font-size:13px; }}
.flag {{ color:var(--flag); font-size:13px; }}
.flag.info {{ color:var(--muted); }} audio {{ width:320px; max-width:100%; }} ul {{ margin:6px 0 0 18px; padding:0; }}
.bar {{ position:sticky; top:0; background:var(--bg); padding:6px 0; border-bottom:1px solid var(--line); }}
</style></head><body>
<div class=bar><b>{esc(dlg)}</b> · {len(says)} lines · click a reply to jump there and play it ·
<label><input type=checkbox id=auto> Auto-continue single replies</label></div>
{''.join(cards)}
<script>
function play(id) {{
  const card = document.getElementById(id); if (!card) return;
  card.scrollIntoView({{block: 'center'}}); location.hash = id;
  document.querySelectorAll('audio').forEach(a => a.pause());
  const a = card.querySelector('audio'); if (a) {{ a.currentTime = 0; a.play().catch(() => {{}}); }}
}}
document.querySelectorAll('a[data-go]').forEach(l => l.addEventListener('click', e => {{
  e.preventDefault(); play(l.dataset.go); }}));
document.querySelectorAll('audio').forEach(a => a.addEventListener('ended', () => {{
  if (!document.getElementById('auto').checked) return;
  const links = a.closest('.card').querySelectorAll('a[data-go]');
  if (links.length === 1) setTimeout(() => play(links[0].dataset.go), 400);
}}));
{f"play('s{says.index(focus)}');" if focus in says else ""}
</script></body></html>"""
    out = out_dir / f"{dlg.upper()}.html"
    out.write_text(page, encoding="utf-8")
    return out


# ==================== CLI ====================

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tone-pass", action="store_true", help="Apply the delivery guard to directions.json")
    parser.add_argument("--conversation", action="append", help="Write the conversation page for this DLG")
    args = parser.parse_args()

    plan = [p for p in read_plan(PLAN_PATH) if p.action == "generate"]
    overrides = load_line_overrides()
    directions = load_directions()
    if args.tone_pass:
        changed, checked = tone_pass(plan, directions, overrides)
        save_directions(directions)
        print(f"Delivery guard: {changed} of {checked} Strata directions changed (originals kept as 'raw')")
    generation = load_generation(FISH_AUDIO_DIR / "generation.json")
    notes = load_line_notes()
    flags = audit(plan, overrides, directions, generation, notes)
    dialog_of = dialog_of_strrefs()
    if args.conversation:
        texts = load_texts()
        for dlg in args.conversation:
            out = conversation_html(dlg, texts, plan, directions, flags)
            print(out or f"No DLG named {dlg}")
        return
    reviewed = load_reviewed()
    write_review_csv(plan, flags, reviewed, dialog_of)
    counts: Dict[str, int] = {}
    for fl in flags.values():
        for code in {f.code for f in fl}:
            counts[code] = counts.get(code, 0) + 1
    queue = sum(1 for s, fl in flags.items() if needs_review(fl, reviewed.get(s)))
    print(f"{len(flags):,} flagged lines, {queue:,} need review -> {REVIEW_CSV}")
    for code, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {code:<11} {SEVERITY.get(code, 'review'):<7} {n:,}")


if __name__ == "__main__":
    main()
