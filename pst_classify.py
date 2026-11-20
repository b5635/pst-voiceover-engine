#!/usr/bin/env python3
"""
PST Line Mood Classifier
Picks which mood reference (from voices_moods_pst/moods.json) each generated line
should use, by asking a local LLM through an OpenAI-compatible API (Ollama,
LM Studio, llama.cpp server, ...).

    python pst_classify.py --speaker Annah --limit 12
    python pst_classify.py --strref 1855 --strref 6807 --url http://127.0.0.1:11434/v1 --model qwen3.5:9b

Results are cached in pst_revoice/moods.csv (StrRef, Speaker, Mood, Reason) and
re-used on later runs; --force re-asks.
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from libs.strata_hosts import auth_headers

from libs.appconfig import cfg
from libs.dlg_tree import history, load_tree
from libs.utils import setup_logging
from pst_revoice import NARRATOR, PLAN_PATH, read_plan, split_segments, voice_for

# Ensure UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

logger = setup_logging(Path(__file__).stem)

# ==================== CONFIGURATION ====================
DEFAULT_LLM_URL = "http://127.0.0.1:8080/v1"    # PolyStrata (run-qwen36-ud-q4_k_m.bat); Ollama :11434/v1, LM Studio :1234/v1
MANIFEST_PATH = Path("voices_moods_pst/moods.json")
PROFILES_PATH = Path("voices_moods_pst/profiles.json")   # {voice: short personality/speech summary}
MOODS_CSV = Path("pst_revoice/moods.csv")
CONTEXT_EXCHANGES = 10     # (NPC line, player choice) pairs walked back up the dialog tree
PLAYER = "Player (The Nameless One)"
REQUEST_TIMEOUT = 300
MAX_TOKENS = 200           # reason is written before the mood, so leave room for it
THINK_MAX_TOKENS = 4096    # --think: Qwen3.x reasons in <think> before answering

SYSTEM_PROMPT = (
    "You are a voice director for the video game Planescape: Torment, casting the delivery of each "
    "dialog line for a voice actor. You are told which character speaks the line, the voice moods "
    "recorded for that character, and the conversation leading up to it.\n\n"
    "How to decide:\n"
    "1. Only the quoted words are spoken aloud; text between asterisks or outside quotes is narration. "
    "Narration describing the speaker's face, tone or actions (e.g. *she sneers*, *his voice drops*) is "
    "the strongest cue.\n"
    "2. Next, read the words themselves and what the player just said or did: a threat, a joke, a "
    "kindness or an insult usually shapes the reply.\n"
    "3. Keep the character's personality in mind: a sharp-tongued character can be sarcastic even when "
    "the words are mild, and a stoic one rarely shouts.\n"
    "4. If nothing clearly points to an emotion, choose the character's default mood rather than "
    "guessing at a dramatic one.\n\n"
    "Answer with JSON only, writing the reason first: "
    '{"reason": "<the decisive cue, max 20 words>", "mood": "<exactly one of the available moods>"}'
)
# =======================================================


def load_manifest(path: Path = MANIFEST_PATH) -> Dict:
    """Read moods.json."""
    return json.loads(path.read_text(encoding="utf-8"))


def load_profiles(path: Path = PROFILES_PATH) -> Dict[str, str]:
    """Read profiles.json, or {} if missing."""
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def build_prompt(speaker: str, text: str, moods: Dict[str, Dict], context: List[Tuple[str, str]],
                 default: str = "", profile: str = "") -> str:
    """
    Compose the user prompt for one line.

    Args:
        speaker: Character speaking the line.
        text: Full line text (speech + narration).
        moods: The character's built moods from moods.json.
        context: (speaker, text) of preceding lines in the same dialog.
        default: The character's default mood, marked in the list.
        profile: Short personality summary of the character, if known.

    Returns:
        Prompt text.
    """
    mood_list = "\n".join(f"- {name}{' (default)' if name == default else ''}: {info['description']}"
                          for name, info in moods.items())
    lines = [f"Character: {speaker}", ""]
    if profile:
        lines += ["Character profile:", profile, ""]
    lines += ["Available moods:", mood_list, ""]
    if context:
        lines.append("Conversation so far (the last line is the player's choice just before this line):")
        lines += [f"{who or 'Unknown'}: {what}" for who, what in context]
        lines.append("")
    lines += [f"Line to voice ({speaker}):", text]
    return "\n".join(lines)


def parse_mood(reply: str, moods: Dict[str, Dict], default: str) -> Tuple[str, str]:
    """
    Pull the mood out of the model's reply, tolerating extra text or thinking blocks.

    Args:
        reply: Raw model output.
        moods: Allowed moods.
        default: Fallback mood.

    Returns:
        (mood, reason); mood is `default` if the reply names no allowed mood.
    """
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S)
    match = re.search(r"\{.*\}", reply, flags=re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            mood = str(data.get("mood", "")).strip().lower()
            if mood in moods:
                return mood, str(data.get("reason", "")).strip()
        except json.JSONDecodeError:
            pass
    for mood in moods:
        if re.search(rf"\b{re.escape(mood)}\b", reply, flags=re.I):
            return mood, "parsed from free text"
    return default, "fallback: no valid mood in reply"


def ask_llm(base_url: str, model: str, prompt: str, think: bool = False, system: str = SYSTEM_PROMPT,
            max_tokens: int = MAX_TOKENS) -> str:
    """Send one chat completion request and return the reply text."""
    resp = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        json={"model": model, "temperature": 0.2, "max_tokens": THINK_MAX_TOKENS if think else max_tokens,
              "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
              # Qwen3.x thinking is off by default: it is slow and only the JSON is read
              "chat_template_kwargs": {"enable_thinking": think}},
        headers=auth_headers(base_url), timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


class DialogContext:
    """Looks up the conversation leading to a line, from the extracted DLG files."""

    def __init__(self, rows: List[Dict[str, str]], dlg_dir: Path):
        self.tree = load_tree(dlg_dir)
        self.lines = {int(r["StrRef"]): (r["RealName"], r["Text"]) for r in rows}

    def get(self, strref: int) -> List[Tuple[str, str]]:
        """[(speaker, text)] for up to CONTEXT_EXCHANGES exchanges before `strref`."""
        result = []
        for role, ref in history(self.tree, strref, CONTEXT_EXCHANGES):
            name, text = self.lines.get(ref, ("", ""))
            result.append((PLAYER if role == "player" else name, text))
        return result


def read_cache() -> Dict[int, Dict[str, str]]:
    """Read pst_revoice/moods.csv."""
    if not MOODS_CSV.exists():
        return {}
    with MOODS_CSV.open("r", encoding="utf-8-sig") as f:
        return {int(r["StrRef"]): r for r in csv.DictReader(f)}


def write_cache(cache: Dict[int, Dict[str, str]]) -> None:
    """Write pst_revoice/moods.csv."""
    MOODS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with MOODS_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["StrRef", "Speaker", "Mood", "Reason"])
        writer.writeheader()
        for strref in sorted(cache):
            writer.writerow(cache[strref])


def classify(base_url: str, model: str, speakers: Optional[List[str]], strrefs: Optional[List[int]],
             limit: int, force: bool, think: bool = False) -> None:
    """Classify planned lines and update the cache."""
    characters = load_manifest()["characters"]
    profiles = load_profiles()
    with open(cfg.CSV_PATH, "r", encoding="utf-8-sig") as f:
        context = DialogContext(list(csv.DictReader(f)), Path(cfg.EXTRACT_DIR))
    cache = read_cache()

    todo = [p for p in read_plan(PLAN_PATH) if p.action == "generate" and voice_for(p.speaker) in characters]
    if speakers:
        todo = [p for p in todo if p.speaker in speakers]
    if strrefs:
        todo = [p for p in todo if p.strref in strrefs]
    if not force:
        todo = [p for p in todo if p.strref not in cache]
    if limit:
        todo = todo[:limit]

    logger.info(f"🧭 Classifying {len(todo)} lines with {model}")
    for idx, row in enumerate(todo, 1):
        entry = characters[voice_for(row.speaker)]
        moods, default = entry["moods"], entry["default"]
        if len(moods) == 1:
            mood, reason = default, "only one reference"
        elif all(voice == NARRATOR for voice, _ in split_segments(row.text, row.speaker)):
            mood, reason = default, "narration only"
        else:
            prompt = build_prompt(row.speaker, row.text, moods, context.get(row.strref), default,
                                  profiles.get(voice_for(row.speaker), ""))
            try:
                mood, reason = parse_mood(ask_llm(base_url, model, prompt, think), moods, default)
            except Exception as e:
                logger.error(f"❌ {row.strref}: {e}")
                continue
        cache[row.strref] = {"StrRef": str(row.strref), "Speaker": row.speaker, "Mood": mood, "Reason": reason}
        logger.info(f"[{idx}/{len(todo)}] {row.speaker} {row.strref}: {mood} - {reason}")
        write_cache(cache)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="qwen3.6-35b-a3b-ud-q4_k_m", help="Model name as the server knows it")
    parser.add_argument("--url", default=DEFAULT_LLM_URL, help="OpenAI-compatible base URL")
    parser.add_argument("--speaker", action="append", help="Only this character (repeatable)")
    parser.add_argument("--strref", type=int, action="append", help="Only this strref (repeatable)")
    parser.add_argument("--limit", type=int, default=0, help="Stop after N lines")
    parser.add_argument("--force", action="store_true", help="Re-classify cached lines")
    parser.add_argument("--think", action="store_true", help="Let the model reason first (slower, more careful)")
    args = parser.parse_args()
    classify(args.url, args.model, args.speaker, args.strref, args.limit, args.force, args.think)


if __name__ == "__main__":
    main()
