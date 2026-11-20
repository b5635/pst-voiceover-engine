#!/usr/bin/env python3
"""
PST Drama 3 Director
Asks a local LLM (PolyStrata Qwen3.6 through its OpenAI-compatible API) to write
Fish Audio Drama 3 performance directions for each line being revoiced, the way
a voice director briefs an actor:

    "Eh?" Annah hisses like a cat. "Are yeh *daft*?! ..."
    -> Annah: [furious hiss, incredulous] Eh?
       Annah: [scornful, rising to a shout] Are yeh <emphasis>daft</emphasis>?! ...

Each speech segment gets a leading [direction], optionally one [event] such as
[sighs] or [scoffs] where the narration calls for it, and <whisper>/<soft>/...
spans on a few words. The words themselves never change: pst_fish.py checks
them and falls back to the direction alone if the model altered any word.

    python pst_direct.py --speaker Morte --limit 10
    python pst_direct.py --strref 7711 --strref 30831 --force
    python pst_direct.py --workers 2            # parallel requests, if the server has slots

Every planned generate line that Drama 3 will speak is directed: silent
main-cast lines and library NPCs (NWN/WoW). Originally voiced lines keep the
game WAV and are skipped. Narration-only lines are skipped (the narrator keeps
its fixed direction).
Directions are free-form; Strata is not given the OmniVoice mood list.

Results are cached in pst_revoice/directions.json (one entry per strref) and
re-used on later runs; --force re-asks. A line whose spoken text, speaker,
voice, or pronunciation no longer matches the cached snapshot is re-directed
without --force. pst_fish.py still ignores director segments whose base text
no longer matches.
"""

import argparse
import csv
import json
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests

from libs.appconfig import cfg
from libs.pst_tone import tone_text
from libs.utils import load_patcher_config, setup_logging
from pst_classify import DEFAULT_LLM_URL, DialogContext, ask_llm, load_profiles, read_cache
from pst_fish import (
    DIRECTIONS_PATH,
    DIRECTOR_PROMPT_VERSION,
    PATCHER_CONFIG_PATH,
    direction_fields,
    direction_stale_reasons,
    drama3_segments,
    load_directions,
    sanitize_directed,
    save_directions,
)
from pst_revoice import (
    CASTING_PATH,
    NARRATOR,
    PLAN_PATH,
    PlanRow,
    dialog_of_strrefs,
    load_line_overrides,
    load_pronunciation,
    read_plan,
    voice_for,
)

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

logger = setup_logging(Path(__file__).stem)

# ==================== CONFIGURATION ====================
DEFAULT_MODEL = "qwen3.6-35b-a3b-ud-q4_k_m"
MAX_TOKENS = 600

SYSTEM_PROMPT = (
    "You are the voice director for a new voice recording of the video game Planescape: Torment. The lines are "
    "performed by Fish Audio Drama 3, a text-to-speech model that clones the character's voice from a recorded "
    "clip and performs each line the way a bracketed direction in the text tells it to. There is no fixed mood "
    "list: invent a fresh direction for this moment from the dialogue, the narration, and the conversation so far.\n\n"
    "You are told the character, their personality when known, the conversation leading up to the line, the full "
    "on-screen line, and the line's speech segments, numbered. Text outside quotes in the on-screen line is "
    "narration read by a separate narrator; it is not yours to direct, but narration describing the speaker "
    "(*she sneers*, *his voice drops*) is the strongest cue for how the speech should sound.\n\n"
    "For each speech segment, write the text to send to the model:\n"
    "1. Start with one direction in square brackets, written like a brief to a voice actor: emotion, intent, "
    "volume, pace, who they are talking to. 3 to 12 words, lowercase, comma-separated. Invent it; do not pick from "
    "a menu. Examples of the style (not a list to choose from): "
    "[dry, mocking, unimpressed] [bitter, through clenched teeth] [nervous, hesitant, keeping it brief] "
    "[softly, voice catching, close to tears] [cocky, gleeful, enjoying himself].\n"
    "Volume: most lines are ordinary conversation at a normal speaking volume. Never ask for shouting, yelling, "
    "screaming, shrill or loud delivery unless the narration says the speaker shouts / yells / screams / roars, "
    "or they are calling for help or fighting. Never ask for whispering, a hushed or low voice unless the "
    "narration says they whisper, murmur, mutter or lower their voice. Fear, anger and urgency are carried by "
    "tone and pace, not volume.\n"
    "Do not describe the voice's texture (raspy, gravelly, guttural, hoarse, nasal): the cloned voice already has "
    "it. Do not describe body actions (leaning in, glancing around): only what can be heard.\n"
    "2. Then the segment's words exactly as given: the same words in the same order with the same spelling. "
    "Copy the spelling from the numbered segment, not from the on-screen line: segments carry pronunciation fixes "
    "(for example 'yeh' becomes 'ye', 'Git' becomes 'Get') and dialect spellings such as 'ta' and 'berk' are "
    "deliberate. Keep every <emphasis> tag; they mark the game's own emphasis.\n"
    "3. Only where the narration or the words clearly call for it, add one sound event in square brackets at the "
    "point it happens: [sighs] [laughs] [chuckles] [scoffs] [snorts] [gasps] [groans] [hisses] [clears throat] "
    "[short pause]. Most segments need none.\n"
    "4. Rarely, wrap a few words, never the whole segment, in <whisper>, <emphasis>, <soft>, <forceful>, <slow>, "
    "<fast> or <stress> when the delivery clearly changes for just those words (a final threat, a word whispered "
    "aside). Do not add tags just to stress a word; most segments need none.\n"
    "5. If the delivery shifts between segments (for example the narration says the voice drops), give the later "
    "segment its own direction.\n\n"
    "Fit the direction to the character's personality and the moment. Don't over-act: if nothing points to a "
    "strong emotion, write a light direction (calm, matter-of-fact, dry) rather than a dramatic one.\n\n"
    "Answer with JSON only, writing the reason first: "
    '{"reason": "<the decisive cue, max 20 words>", "segments": [{"i": <segment number>, "text": "<directed text>"}]}\n'
    "The reply must be valid JSON matching this schema: "
    '{"type": "object", "required": ["reason", "segments"], "properties": {"reason": {"type": "string"}, '
    '"segments": {"type": "array", "items": {"type": "object", "required": ["i", "text"], '
    '"properties": {"i": {"type": "integer"}, "text": {"type": "string"}}}}}}\n'
    "Inside any JSON string, write every double quotation mark as \\\" (backslash, quote). Lines often quote "
    "words ('He calls it \\\"the Hive\\\"'); an unescaped quote breaks the JSON."
)
ERRORS_PATH = Path("pst_revoice/direct_errors.jsonl")   # replies that failed to parse even after a retry
# =======================================================


def format_cast_profile(entry: Dict) -> str:
    """A short personality blurb from a pst_cast.py casting.json record."""
    profile = entry.get("profile") or {}
    parts = [profile.get("personality"), profile.get("voice"), profile.get("appearance")]
    return " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())


def load_cast_profiles(path: Path = CASTING_PATH) -> Dict[str, str]:
    """speaker -> personality blurb from pst_cast/casting.json."""
    if not path.exists():
        return {}
    result: Dict[str, str] = {}
    for entry in json.loads(path.read_text(encoding="utf-8")).values():
        speaker = str(entry.get("speaker") or "").strip()
        text = format_cast_profile(entry)
        if speaker and text:
            result[speaker] = text
    return result


GITH_NOTE = ("Githzerai are austere, disciplined and unemotional: keep the delivery even, restrained and close "
             "to monotone; let small shifts in tone carry the emotion.")


def githzerai_speakers(path: Path = CASTING_PATH) -> Set[str]:
    """
    Speakers (and DLG names, for speakers without a name) cast as githzerai.
    Dak'kon has his own voice and direction style and is never included.
    """
    found: Set[str] = set()
    if path.exists():
        for dlg, entry in json.loads(path.read_text(encoding="utf-8")).items():
            kind = str((entry.get("profile") or {}).get("type", "")).lower()
            speaker = str(entry.get("speaker") or "").strip()
            if "githzerai" in kind or "githzerai" in speaker.lower():
                found |= {speaker or dlg, dlg}
    return {s for s in found if s and "dak'kon" not in s.lower()}


def is_githzerai(row: PlanRow, gith: Set[str], dialog_of: Dict[int, str]) -> bool:
    return row.speaker in gith or dialog_of.get(row.strref, "").upper() in gith


def toned_segments(segments: List[Dict], line: str, githzerai: bool) -> Tuple[List[Dict], List[str]]:
    """
    Apply libs.pst_tone to directed segments ({"text", ...}); keeps the
    director's original in "raw" when it changed. Returns (segments, changes).
    """
    out, changes = [], []
    for seg in segments:
        raw = seg.get("raw", seg["text"])
        text, changed = tone_text(raw, line, githzerai)
        new = {k: v for k, v in seg.items() if k != "raw"}
        new["text"] = text
        if text != raw:
            new["raw"] = raw
        out.append(new)
        changes += changed
    return out, changes


def lines_to_direct(plan: Iterable[PlanRow], speakers: Optional[List[str]] = None,
                    strrefs: Optional[List[int]] = None, already: Optional[Set[int]] = None,
                    force: bool = False, limit: int = 0,
                    stale: Optional[Set[int]] = None) -> List[PlanRow]:
    """
    Planned lines the director should write notes for.

    Every generate row that Drama 3 will speak is eligible (silent main-cast
    lines and library NPCs). Originally voiced lines keep the game WAV and are
    skipped. Already-directed strrefs are skipped unless force or the strref is
    in stale (spoken / speaker / pronunciation no longer match the
    cached snapshot). Narration-only lines are still listed here; work() drops
    them when they have no speech segment.
    """
    todo = [p for p in plan if p.action == "generate" and not p.original]
    if speakers:
        wanted = set(speakers)
        todo = [p for p in todo if p.speaker in wanted]
    if strrefs:
        order = {s: i for i, s in enumerate(strrefs)}
        todo = sorted((p for p in todo if p.strref in order), key=lambda p: order[p.strref])
    if not force and already:
        stale = stale or set()
        todo = [p for p in todo if p.strref not in already or p.strref in stale]
    if limit:
        todo = todo[:limit]
    return todo


def build_prompt(speaker: str, profile: str, line: str,
                 context: List, segments: List[Dict]) -> str:
    """
    Compose the user prompt for one line.

    Args:
        speaker: Character speaking the line.
        profile: Short personality summary, if known.
        line: Full on-screen line (speech + narration).
        context: (speaker, text) of the preceding lines.
        segments: [{"i", "voice", "base"}] of the line, narration included for reading order.

    Returns:
        Prompt text.
    """
    lines = [f"Character: {speaker}"]
    if profile:
        lines += ["", "Character profile:", profile]
    if context:
        lines += ["", "Conversation so far (the last line is the player's choice just before this line):"]
        lines += [f"{who or 'Unknown'}: {what}" for who, what in context]
    lines += ["", "On-screen line:", line, "", "Segments (direct only the speech ones):"]
    for seg in segments:
        kind = "narration, read by the narrator" if seg["voice"] == NARRATOR else "speech"
        lines.append(f"{seg['i']} ({kind}): {seg['base']}")
    return "\n".join(lines)


def parse_reply(reply: str) -> Dict:
    """Pull the JSON object out of the reply, tolerating thinking blocks and extra text."""
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S)
    match = re.search(r"\{.*\}", reply, flags=re.S)
    if not match:
        raise ValueError(f"no JSON in reply: {reply[:200]}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        # Quote-heavy lines make the model emit unescaped quotes: salvage each segment's text field
        segments = [{"i": int(i), "text": text.replace('\\"', '"')}
                    for i, text in re.findall(r'"i"\s*:\s*(\d+)\s*,\s*"text"\s*:\s*"(.*?)"\s*\}', reply, flags=re.S)]
        if not segments:
            raise
        reason = re.search(r'"reason"\s*:\s*"(.*?)"\s*,', reply, flags=re.S)
        return {"reason": reason.group(1) if reason else "", "segments": segments}


class DirectError(ValueError):
    """A reply that would not parse even after asking again; carries the raw replies for the error log."""

    def __init__(self, message: str, replies: List[str]):
        super().__init__(message)
        self.replies = replies


def log_direct_error(strref: int, error: Exception, path: Optional[Path] = None) -> None:
    """Append a failed line (error + raw replies) to pst_revoice/direct_errors.jsonl."""
    path = path or ERRORS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"strref": strref, "error": str(error), "replies": getattr(error, "replies", []),
              "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def direct_line(base_url: str, model: str, prompt: str, speech: List[Dict], think: bool) -> Dict:
    """
    Ask for one line's directions; asks once more if any segment's words were changed.

    Returns:
        {"reason", "mood" (may be ""), "segments": {i: text}, "valid": bool}.
    """
    ask = prompt
    for attempt in range(2):
        reply = ask_llm(base_url, model, ask, think, system=SYSTEM_PROMPT, max_tokens=MAX_TOKENS)
        try:
            data = parse_reply(reply)
        except (ValueError, KeyError) as e:     # json.JSONDecodeError is a ValueError
            reply2 = ask_llm(base_url, model, ask + f"\n\nYour previous answer was not valid JSON ({e}). "
                             "Answer again with valid JSON only, escaping every double quote inside strings "
                             "as \\\".", think, system=SYSTEM_PROMPT, max_tokens=MAX_TOKENS)
            try:
                data = parse_reply(reply2)
            except (ValueError, KeyError) as e2:
                raise DirectError(str(e2), [reply, reply2]) from e2
        texts = {int(s["i"]): str(s.get("text", "")) for s in data.get("segments", []) if "i" in s}
        bad = [seg["i"] for seg in speech if not sanitize_directed(seg["base"], texts.get(seg["i"], ""))[1]]
        if not bad or attempt == 1:
            return {"reason": str(data.get("reason", "")).strip(), "mood": str(data.get("mood", "")).strip().lower(),
                    "segments": texts, "valid": not bad}
        ask = (prompt + f"\n\nYour previous answer changed the words of segment(s) {bad} or missed them. "
               "Copy each segment's words exactly and only add the direction and tags.")
    raise AssertionError("unreachable")


def progress_line(done: int, total: int, elapsed: float, alive: int, servers: int, done_by: Dict[str, int]) -> str:
    """Machine-readable progress for the GUI: servers up, lines done, ETA seconds, lines per server."""
    eta = int(elapsed / done * (total - done)) if done else 0
    per = ",".join(f"{u.split('//')[-1].split('/')[0]}={n}" for u, n in done_by.items())
    return f"📡 DIRECT|{alive}|{servers}|{done}|{total}|{eta}|{per}"


def direct(base_url: str, model: str, speakers: Optional[List[str]], strrefs: Optional[List[int]], limit: int,
           force: bool, think: bool, workers: int, out_path: Path,
           servers: Optional[List[Tuple[str, str]]] = None) -> None:
    """
    Write directions for planned generate lines that Drama 3 will speak.

    servers: [(openai base url, model)] to spread lines over, `workers` parallel requests each
    (default: just base_url/model).
    """
    servers = servers or [(base_url, model)]
    profiles = load_profiles()
    cast_profiles = load_cast_profiles()
    picks = read_cache()
    patcher_config = load_patcher_config(PATCHER_CONFIG_PATH)
    pronunciation = load_pronunciation()
    overrides = load_line_overrides()
    with open(cfg.CSV_PATH, "r", encoding="utf-8-sig") as f:
        context = DialogContext(list(csv.DictReader(f)), Path(cfg.EXTRACT_DIR))
    directions = load_directions(out_path)
    plan = read_plan(PLAN_PATH)
    gith_speakers = githzerai_speakers()
    dialog_of = dialog_of_strrefs()
    stale: Set[int] = set()
    wanted_refs = set(strrefs or [])
    if not force:
        for row in plan:
            if row.action != "generate" or row.strref not in directions:
                continue
            if speakers and row.speaker not in speakers:
                continue
            if wanted_refs and row.strref not in wanted_refs:
                continue
            text = overrides.get(row.strref, row.text)
            reasons = direction_stale_reasons(row, text, patcher_config, pronunciation, directions[row.strref])
            if reasons:
                stale.add(row.strref)
                if len(stale) <= 15:
                    logger.info(f"🔄 {row.speaker} {row.strref} direction stale: {'; '.join(reasons)}")
    todo = lines_to_direct(plan, speakers, strrefs, set(directions), force, limit, stale)
    if stale:
        extra = f" ({len(stale) - 15} more)" if len(stale) > 15 else ""
        logger.info(f"🔄 {len(stale)} directed lines are stale and will be re-directed{extra}")

    lock = threading.Lock()
    logger.info(f"🎬 Directing {len(todo)} lines with " + ", ".join(f"{m} @ {u}" for u, m in servers))
    # One token per parallel request slot; a server that stops answering loses its tokens.
    slots: "queue.Queue[Tuple[str, str]]" = queue.Queue()
    for server in servers:
        for _ in range(max(1, workers)):
            slots.put(server)
    dead: Set[str] = set()
    done_by: Dict[str, int] = {u: 0 for u, _ in servers}
    started = time.monotonic()

    def work(row) -> Optional[str]:
        text = overrides.get(row.strref, row.text)
        segments = [{"i": i, "voice": v, "base": b}
                    for i, (v, b) in enumerate(drama3_segments(text, row, patcher_config, pronunciation))]
        speech = [s for s in segments if s["voice"] != NARRATOR]
        if not speech:
            return None
        cloned = voice_for(row.speaker)
        profile = profiles.get(cloned, "") or profiles.get(row.speaker, "") or cast_profiles.get(row.speaker, "")
        # The voice clone is left out on purpose: directions follow the character,
        # so recasting a voice does not invalidate them.
        gith = is_githzerai(row, gith_speakers, dialog_of)
        if gith:
            profile = f"{profile} {GITH_NOTE}".strip()
        prompt = build_prompt(row.speaker, profile, text, context.get(row.strref), segments)
        while True:
            try:
                url, used_model = slots.get(timeout=5)
            except queue.Empty:
                if len(dead) >= len(servers):
                    raise RuntimeError("every Strata server stopped answering")
                continue
            if url in dead:
                continue
            try:
                result = direct_line(url, used_model, prompt, speech, think)
                break
            except requests.ConnectionError:
                with lock:
                    dead.add(url)
                logger.error(f"Strata {url} stopped answering; moving its lines to the other server(s)")
            finally:
                if url not in dead:
                    slots.put((url, used_model))
        directed, toned = toned_segments([{"voice": s["voice"], "base": s["base"],
                                           "text": result["segments"].get(s["i"], s["base"])} for s in speech],
                                         text, gith)
        record = {**direction_fields(row, text, patcher_config, pronunciation),
                  "source": f"strata:{used_model}", "prompt_version": DIRECTOR_PROMPT_VERSION,
                  "mood": picks.get(row.strref, {}).get("Mood", ""), "reason": result["reason"],
                  "valid": result["valid"], "segments": directed}
        if toned:
            record["toned"] = toned
        with lock:
            directions[row.strref] = record
            done_by[url] += 1
            save_directions(directions, out_path)
        shown = " | ".join(seg["text"] for seg in record["segments"])
        return f"{row.speaker} {row.strref}{'' if result['valid'] else ' ⚠ words changed'}: {shown[:160]}"

    with ThreadPoolExecutor(max_workers=max(1, workers) * len(servers)) as pool:
        futures = {pool.submit(work, row): row for row in todo}
        for idx, future in enumerate(as_completed(futures), 1):
            row = futures[future]
            try:
                message = future.result()
            except Exception as e:
                logger.error(f"[{idx}/{len(todo)}] ❌ {row.strref}: {e} (raw replies in {ERRORS_PATH})")
                log_direct_error(row.strref, e)
                continue
            if message:
                logger.info(f"[{idx}/{len(todo)}] {message}")
            logger.info(progress_line(idx, len(todo), time.monotonic() - started, len(servers) - len(dead),
                                      len(servers), done_by))


def ready_servers(url: str, model: str, single: bool = False) -> List[Tuple[str, str]]:
    """Wake the local Strata (starting it if needed) and the extra servers; [(openai url, model)] that are ready."""
    from libs.strata_hosts import base_of, load_hosts, wake_host
    local = base_of(url)
    hosts = [local] if single else load_hosts(url)
    ready: List[Tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:     # servers load in parallel
        def wake(base: str):
            start = None
            if base == local and base.split("//")[-1].split(":")[0] in ("127.0.0.1", "localhost"):
                from pst_pipeline import start_strata_server
                start = start_strata_server
            return wake_host(base, logger.info, start)
        for result in pool.map(wake, hosts):
            if result:
                ready.append((f"{result[0]}/v1", result[1]))
    return ready


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name as the server knows it")
    parser.add_argument("--url", default=DEFAULT_LLM_URL, help="OpenAI-compatible base URL")
    parser.add_argument("--speaker", action="append", help="Only this character (repeatable)")
    parser.add_argument("--strref", type=int, action="append", help="Only this strref (repeatable)")
    parser.add_argument("--strref-file", type=Path, help="Only the strrefs listed in this file (whitespace-separated)")
    parser.add_argument("--limit", type=int, default=0, help="Stop after N lines")
    parser.add_argument("--force", action="store_true", help="Re-direct cached lines")
    parser.add_argument("--think", action="store_true", help="Let the model reason first (slower, more careful)")
    parser.add_argument("--workers", type=int, default=1, help="Parallel requests (needs server slots)")
    parser.add_argument("--out", type=Path, default=DIRECTIONS_PATH, help="Directions file to write")
    parser.add_argument("--single", action="store_true", help="Only the --url server, not the extras in pst-strata.json")
    parser.add_argument("--no-wake", action="store_true", help="Do not start/load servers; just call --url")
    args = parser.parse_args()
    strrefs = list(args.strref or [])
    if args.strref_file:
        strrefs += [int(x) for x in args.strref_file.read_text(encoding="utf-8").split()]
        if not strrefs:
            logger.info("Strref file is empty; nothing to direct")
            return
    servers = [(args.url, args.model)]
    if not args.no_wake:
        servers = ready_servers(args.url, args.model, args.single)
        if not servers:
            logger.error("No Strata server is reachable. Start PolyStrata (local or the one in pst-strata.json).")
            sys.exit(1)
    direct(args.url, args.model, args.speaker, strrefs or None, args.limit, args.force, args.think, args.workers,
           args.out, servers)


if __name__ == "__main__":
    main()
