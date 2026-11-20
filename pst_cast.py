#!/usr/bin/env python3
"""
PST Voice Casting
Has a local LLM (Strata / PolyStrata, OpenAI-compatible) pick a library voice
for every unvoiced Planescape: Torment NPC dialog.

Two steps per dialog, both cached in pst_cast/casting.json (resumable):

    1. profile  - from the creatures that use the dialog (name, gender, race,
                  animation/appearance) and every NPC line of it (player replies
                  left out), the LLM describes the speaker: gender, type, age,
                  appearance, personality and how they should sound.
    2. cast     - given that profile and the voice library entries of a fitting
                  gender (NWN soundsets + WoW NPC voices, voice_library/*/catalog.json),
                  the LLM picks one voice id.

Dialogs whose speaker already has a voice (Morte, Annah, ...) and dialogs that
are narration only are skipped.

Usage (start the LLM server first, or click Cast NPCs in run_pst.bat):
    python pst_cast.py                      every dialog not cast yet (or breaking a rule)
    python pst_cast.py --dry-run            list what would be (re)cast
    python pst_cast.py --no-llm             only dialogs a rule pins to one voice
    python pst_cast.py --dialog DPHAROD     one dialog (repeatable)
    python pst_cast.py --limit 20 --force   re-cast the first 20
"""

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# ==================== CONFIGURATION ====================
DEFAULT_LLM_URL = "http://127.0.0.1:8080/v1"       # PolyStrata (run-qwen36-ud-q4_k_m.bat)
DEFAULT_MODEL = "qwen3.6-35b-a3b-ud-q4_k_m"
NPCS_PATH = Path("pst_cast/npcs.json")
CASTING_PATH = Path("pst_cast/casting.json")
CATALOGS = [Path("voice_library/nwn/catalog.json"), Path("voice_library/wow/catalog.json")]
RULES_PATH = Path("pst-cast-rules.json")   # hand rules: excluded voices, voices kept for some NPCs, forced races
MAX_DIALOG_CHARS = 14000       # ~3.5k tokens of NPC lines per prompt
MAX_CREATURE_ROWS = 12
REQUEST_TIMEOUT = 600
PROFILE_MAX_TOKENS = 400
CAST_MAX_TOKENS = 500
OVERUSE = 6                    # a voice cast this often is flagged to the model

PROFILE_SYSTEM = (
    "You are a casting director for the video game Planescape: Torment (AD&D Planescape setting, the city "
    "of Sigil). You are given the creatures that use one conversation file and every line that NPC says in "
    "it (the player's replies are left out). Text in quotes is spoken; other text is narration that often "
    "describes the speaker's looks and voice - narration about appearance and voice is your strongest clue. "
    "The creature's in-game animation (e.g. 'Dustman, Female', 'Zombie, Male', 'Townie, UC Male' = upper-class "
    "townsperson, 'LC' = lower-class) tells you the body type and usually the gender. Gang and faction "
    "names (Razor Angels, Dustmen, Godsmen, ...) describe membership, not species.\n"
    "Describe the speaker for a voice actor. Answer with JSON only:\n"
    '{"speaker": "<name or role>", "gender": "male|female", '
    '"type": "<race/creature and role, e.g. human noble, githzerai monk, zombie, fiend, dabus, object>", '
    '"age": "child|young|adult|old|ageless", "appearance": "<max 25 words>", '
    '"personality": "<max 20 words>", "voice": "<max 20 words: pitch, accent, texture, pace>"}'
)

CAST_SYSTEM = (
    "You are a casting director picking a recorded voice for a Planescape: Torment NPC. You get the NPC's "
    "profile and a list of available voices (id | race | description | times already cast). Pick the one "
    "voice that best matches the NPC's gender, age, type, personality and described voice. Race in the list "
    "is only a hint for how the voice sounds (e.g. a gruff dwarf voice suits a gruff human laborer, an "
    "undead voice suits a zombie or ghoul, a fiendish or monstrous voice suits a demon). Major named "
    "characters deserve a distinctive voice; when two voices fit equally, prefer the one cast fewer times. "
    "Answer with JSON only, reason first:\n"
    '{"reason": "<max 25 words>", "voice_id": "<exactly one id from the list>"}'
)
# =======================================================


def load_catalog() -> List[Dict]:
    """
    Library voices the LLM may cast. Planescape: Torment's own voices (Morte,
    Annah, Dak'kon, ...) belong to their characters and are never offered.
    """
    voices = []
    for path in CATALOGS:
        if path.exists():
            voices += json.loads(path.read_text(encoding="utf-8"))
    return [v for v in voices if "planescape" not in v.get("source", "").lower()
            and not v["id"].lower().startswith("pst:")]


def read_cache() -> Dict[str, Dict]:
    return json.loads(CASTING_PATH.read_text(encoding="utf-8")) if CASTING_PATH.exists() else {}


def write_cache(cache: Dict[str, Dict]) -> None:
    CASTING_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CASTING_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CASTING_PATH)


def creature_summary(npc: Dict) -> List[str]:
    """One row per distinct creature description, most common first."""
    rows = Counter((c["name"] or "?", c["gender"], c["race"], c["class"], c["animation"], c.get("faction", ""),
                    c.get("alignment", ""), ", ".join(c.get("areas", [])[:3])) for c in npc["creatures"])
    out = []
    for (name, g, r, k, a, fac, al, ar), n in rows.most_common(MAX_CREATURE_ROWS):
        row = f"- {name} | gender: {g} | race: {r or '?'} | class: {k or '?'} | animation: {a}"
        row += f" | faction: {fac}" if fac else ""
        row += f" | alignment: {al}" if al else ""
        row += f" | found in: {ar}" if ar else ""
        out.append(row + (f" (x{n})" if n > 1 else ""))
    if len(rows) > MAX_CREATURE_ROWS:
        out.append(f"- ... and {len(rows) - MAX_CREATURE_ROWS} more kinds of creature")
    return out


def dialog_text(npc: Dict, limit: int = MAX_DIALOG_CHARS) -> str:
    """The NPC's lines, one per line, cut at `limit` characters."""
    out, size = [], 0
    for ln in npc["lines"]:
        text = re.sub(r"\s+", " ", ln["text"])
        if size + len(text) > limit:
            out.append(f"[... {npc['line_count'] - len(out)} more lines cut]")
            break
        out.append(text)
        size += len(text) + 1
    return "\n".join(out)


def build_profile_prompt(npc: Dict) -> str:
    lines = [f"Conversation file: {npc['dialog']}", f"Speaker name in the game: {npc['speaker'] or 'unknown'}", ""]
    creatures = creature_summary(npc)
    lines += ["Creatures that use this conversation:"] + (creatures or ["- none found (started by a script)"])
    if npc.get("gender"):
        lines += ["", f"Gender evidence: {npc['gender']} ({npc['gender_source']}). Use it unless the lines clearly "
                  "say otherwise; if still unsure, choose male."]
    lines += ["", "Everything the NPC says (in order, player replies left out):", dialog_text(npc)]
    return "\n".join(lines)


def candidates_for(profile: Dict, voices: List[Dict], fallback: str = "male") -> List[Dict]:
    """Voices of the profile's gender; a neutral/unknown speaker uses `fallback` (the game's gender, else male)."""
    gender = str(profile.get("gender", "")).lower()
    if gender not in ("male", "female"):
        gender = fallback if fallback in ("male", "female") else "male"
    return [v for v in voices if v["gender"] == gender]


def load_rules(path: Path = RULES_PATH) -> Dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


SPIRIT_NARRATION_RE = re.compile(r"\bthe (spirit|ghost|spectre|specter|shade|phantom|apparition|wraith)\b", re.I)
SPIRIT_NARRATION_MIN = 3       # narration calling the speaker "the spirit" this often marks a ghost / possessed corpse


def npc_terms(npc: Dict, profile: Dict) -> str:
    """
    Lower-cased text the rules' terms are matched against: game name, profile
    type/appearance, creature names/animations/races. A dialog whose narration
    keeps calling the speaker "the spirit" / "the ghost" (a spirit possessing a
    corpse) also gets that word.
    """
    parts = [npc.get("speaker", ""), str(profile.get("type", "")), str(profile.get("appearance", ""))]
    for c in npc.get("creatures", []):
        parts += [c.get("name", ""), c.get("animation", ""), c.get("race", "")]
    hits = SPIRIT_NARRATION_RE.findall(" ".join(ln.get("text", "") for ln in npc.get("lines", [])))
    if len(hits) >= SPIRIT_NARRATION_MIN:
        parts.append(Counter(h.lower() for h in hits).most_common(1)[0][0])
    return " ".join(parts).lower()


def term_match(terms: List[str], text: str) -> bool:
    """True if any term starts a word in `text` ("ghost" matches "ghostly", "rat" does not match "pirate")."""
    return any(re.search(r"(?<![a-z])" + re.escape(t.lower()), text) for t in terms)


def _gendered(pool: List[Dict], gender: str) -> List[Dict]:
    return [v for v in pool if v["gender"] == gender] or pool


def apply_rules(candidates: List[Dict], npc: Dict, profile: Dict, voices: List[Dict], rules: Dict) -> List[Dict]:
    """
    Narrow the candidate voices with pst-cast-rules.json, in this order:

    force_voices  {"when", "voices", "gender"?}: the pool becomes exactly these
                  voices (first matching rule wins; same gender if possible).
    force_races   {"when", "races"}: the pool becomes these races' voices.
    avoid_races   {"when", "races"}: drop these races' voices for matching NPCs.
    exclude_voices: never cast (child voices, ...).
    only_for      voice id -> terms: the voice is kept only for NPCs matching a term
                  (a forced voice is always kept).
    """
    text = npc_terms(npc, profile)
    gender = candidates[0]["gender"] if candidates else str(profile.get("gender", "male")).lower()
    by_id = {v["id"]: v for v in voices}
    forced: set = set()
    for rule in rules.get("force_voices", []):
        if "when" not in rule or not term_match(rule["when"], text):
            continue
        if rule.get("gender") and rule["gender"] != gender:
            continue
        pool = [by_id[i] for i in rule["voices"] if i in by_id]
        if pool:
            candidates = _gendered(pool, gender)
            forced = {v["id"] for v in candidates}
            break
    if not forced:
        for rule in rules.get("force_races", []):
            if term_match(rule["when"], text):
                races = {r.lower() for r in rule["races"]}
                pool = [v for v in voices if v.get("race", "").lower() in races]
                gendered = [v for v in pool if v in candidates]
                candidates = gendered or pool
        for rule in rules.get("avoid_races", []):
            if term_match(rule["when"], text):
                races = {r.lower() for r in rule["races"]}
                candidates = [v for v in candidates if v.get("race", "").lower() not in races] or candidates
    excluded = set(rules.get("exclude_voices", []))
    only_for = {k: v for k, v in rules.get("only_for", {}).items() if not k.startswith("_")}
    race_terms = {k.lower(): v for k, v in rules.get("only_for_races", {}).items() if not k.startswith("_")}

    def race_ok(v: Dict) -> bool:
        """only_for_races: "wow:Goblin" -> terms; a library's race voices only for matching NPCs."""
        key = f"{v['id'].split(':', 1)[0]}:{v.get('race', '')}".lower()
        return key not in race_terms or term_match(race_terms[key], text)

    return [v for v in candidates if v["id"] not in excluded
            and (v["id"] in forced or (
                (v["id"] not in only_for or term_match(only_for[v["id"]], text))
                and (v["id"] in only_for or race_ok(v))))]


def build_cast_prompt(npc: Dict, profile: Dict, candidates: List[Dict], usage: Counter) -> str:
    lines = ["NPC profile:"] + [f"- {k}: {v}" for k, v in profile.items()]
    sample = [re.sub(r"\s+", " ", ln["text"]) for ln in npc["lines"] if '"' in ln["text"]][:3]
    if sample:
        lines += ["", "Sample lines:"] + [f"- {s[:300]}" for s in sample]
    lines += ["", "Available voices:"]
    for v in candidates:
        used = usage.get(v["id"], 0)
        flag = f" | cast {used}x" + (" (overused)" if used >= OVERUSE else "") if used else ""
        lines.append(f"{v['id']} | {v['race'] or '?'} | {v['description']}{flag}")
    return "\n".join(lines)


def ask_llm(base_url: str, model: str, system: str, prompt: str, max_tokens: int) -> str:
    resp = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        json={"model": model, "temperature": 0.3, "max_tokens": max_tokens,
              "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
              "chat_template_kwargs": {"enable_thinking": False}},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"] or ""


def parse_json(reply: str) -> Optional[Dict]:
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S)
    match = re.search(r"\{.*\}", reply, flags=re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def pick_voice(reply: str, candidates: List[Dict]) -> Tuple[Optional[str], str]:
    # Match ids ignoring case and a missing "nwn:"/"wow:" prefix ("fthugf_pcv" -> "nwn:fthugf_pcv")
    lookup = {}
    for v in candidates:
        lookup[v["id"].lower()] = v["id"]
        lookup.setdefault(v["id"].split(":", 1)[-1].lower(), v["id"])
    data = parse_json(reply) or {}
    asked = str(data.get("voice_id", "")).strip().lower()
    # Exact id first, then the name alone (the model sometimes writes "wow:Sobrey" for "nwn:Sobrey")
    vid = lookup.get(asked) or lookup.get(asked.split(":", 1)[-1])
    if vid:
        return vid, str(data.get("reason", "")).strip()
    low = reply.lower()
    for key in sorted(lookup, key=len, reverse=True):     # id mentioned in free text
        if ":" in key and key in low:
            return lookup[key], "parsed from free text"
    return None, f"no valid voice id in reply: {reply[-200:]}"


def targets(npcs: List[Dict], dialogs: Optional[List[str]]) -> List[Dict]:
    todo = [n for n in npcs if not n["already_voiced"] and not n["narration_only"]]
    if dialogs:
        wanted = {d.upper() for d in dialogs}
        todo = [n for n in npcs if n["dialog"] in wanted]
    return todo


_BREAKS_CACHE: Dict[tuple, List[str]] = {}


def rule_breaks() -> List[str]:
    """
    Cast dialogs whose voice pst-cast-rules.json no longer allows (child voices,
    Deekin outside wererats / Lim-Lims, ...). Cast NPCs re-casts them. Cached by file mtimes.
    """
    paths = (NPCS_PATH, CASTING_PATH, RULES_PATH, *CATALOGS)
    key = tuple(p.stat().st_mtime if p.exists() else 0 for p in paths)
    if key not in _BREAKS_CACHE:
        if not (NPCS_PATH.exists() and CASTING_PATH.exists()):
            return []
        npcs = json.loads(NPCS_PATH.read_text(encoding="utf-8"))
        voices, cache, rules = load_catalog(), read_cache(), load_rules()
        out = []
        for n in targets(npcs, None):
            c = cache.get(n["dialog"]) or {}
            if not c.get("voice_id"):
                continue
            profile = c.get("profile") or {}
            pool = apply_rules(candidates_for(profile, voices, n.get("gender", "male")), n, profile, voices, rules)
            if c["voice_id"] not in {v["id"] for v in pool}:
                out.append(n["dialog"])
        _BREAKS_CACHE.clear()
        _BREAKS_CACHE[key] = out
    return _BREAKS_CACHE[key]


def cast(base_url: str, model: str, dialogs: Optional[List[str]], limit: int, force: bool,
         no_llm: bool = False, dry_run: bool = False) -> None:
    npcs = json.loads(NPCS_PATH.read_text(encoding="utf-8"))
    voices = load_catalog()
    if not voices:
        sys.exit("No voice library found - run voicelib_nwn.py / voicelib_wow.py first")
    by_id = {v["id"]: v for v in voices}
    cache = read_cache()
    usage = Counter(c["voice_id"] for c in cache.values() if c.get("voice_id"))

    rules = load_rules()

    def allowed(n: Dict, profile: Dict) -> List[Dict]:
        return apply_rules(candidates_for(profile, voices, n.get("gender", "male")), n, profile, voices, rules)

    def stale(n: Dict) -> bool:
        """Not cast yet, cast from info that has changed since, or the pick breaks a rule / left the library."""
        c = cache.get(n["dialog"], {})
        if not c.get("voice_id") or c.get("profile_prompt") != build_profile_prompt(n):
            return True
        return c["voice_id"] not in {v["id"] for v in allowed(n, c.get("profile") or {})}

    todo = [n for n in targets(npcs, dialogs) if force or stale(n)]
    if limit:
        todo = todo[:limit]
    if dry_run:
        for n in todo:
            c = cache.get(n["dialog"], {})
            pool = allowed(n, c.get("profile") or {})
            print(f"  {n['dialog']:<9} {n['speaker'][:24]:<24} {c.get('voice_id') or '-':<28} -> "
                  + (pool[0]["id"] if len(pool) == 1 else f"LLM picks from {len(pool)}"))
        print(f"{len(todo)} dialogs would be (re)cast")
        return
    print(f"Casting {len(todo)} dialogs with {model} from {len(voices)} voices")

    skipped = 0
    for i, npc in enumerate(todo, 1):
        start = time.time()
        profile_prompt = build_profile_prompt(npc)
        cached = cache.get(npc["dialog"], {})
        # A profile written from the same prompt is reused (a rules change only re-picks the voice)
        reuse = not force and cached.get("profile") and cached.get("profile_prompt") == profile_prompt
        if no_llm and not (reuse and len(allowed(npc, cached["profile"])) == 1):
            skipped += 1
            continue
        try:
            profile = cached["profile"] if reuse else parse_json(
                ask_llm(base_url, model, PROFILE_SYSTEM, profile_prompt, PROFILE_MAX_TOKENS))
            if not profile:
                raise ValueError("profile reply was not JSON")
            candidates = allowed(npc, profile)
            cast_prompt = build_cast_prompt(npc, profile, candidates, usage)
            if len(candidates) == 1:     # a rule leaves one voice: nothing to ask
                vid, reason = candidates[0]["id"], "only voice allowed by pst-cast-rules.json"
            else:
                reply = ask_llm(base_url, model, CAST_SYSTEM, cast_prompt, CAST_MAX_TOKENS)
                vid, reason = pick_voice(reply, candidates)
            if not vid:
                # One retry, telling the model its id was not in the list
                retry = (cast_prompt + f"\n\nYour previous answer was not valid: {reply.strip()[-300:]}\n"
                         "That voice_id is not in the list above. Copy one id exactly as written in the list.")
                reply = ask_llm(base_url, model, CAST_SYSTEM, retry, CAST_MAX_TOKENS)
                vid, reason = pick_voice(reply, candidates)
        except Exception as e:
            print(f"  [{i}/{len(todo)}] {npc['dialog']}: ERROR {e}")
            continue
        old = cache.get(npc["dialog"], {}).get("voice_id")
        if old:
            usage[old] -= 1
        if vid:
            usage[vid] += 1
        cache[npc["dialog"]] = {
            "dialog": npc["dialog"], "speaker": npc["speaker"], "profile": profile,
            "voice_id": vid, "voice_name": by_id[vid]["name"] if vid else "", "reason": reason,
            "candidates": len(candidates), "model": model,
            "profile_prompt": profile_prompt, "cast_prompt_chars": len(cast_prompt),
        }
        write_cache(cache)
        print(f"  [{i}/{len(todo)}] {npc['dialog']:<9} {npc['speaker'][:24]:<24} "
              f"{profile.get('gender', '?'):<7} {str(profile.get('type', ''))[:28]:<28} -> {vid}  "
              f"({time.time() - start:.0f}s)")
    if skipped:
        print(f"{skipped} dialogs need the LLM (Cast NPCs with Strata loaded)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_LLM_URL, help="OpenAI-compatible base URL")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name as the server knows it")
    parser.add_argument("--dialog", action="append", help="Only this DLG resref (repeatable)")
    parser.add_argument("--limit", type=int, default=0, help="Stop after N dialogs")
    parser.add_argument("--force", action="store_true", help="Re-cast dialogs already cast")
    parser.add_argument("--no-llm", action="store_true",
                        help="Only re-cast dialogs a rule pins to one voice (cached profile; no LLM needed)")
    parser.add_argument("--dry-run", action="store_true", help="List what would be (re)cast and stop")
    args = parser.parse_args()
    cast(args.url, args.model, args.dialog, args.limit, args.force, args.no_llm, args.dry_run)


if __name__ == "__main__":
    main()
