"""
Delivery guard for Drama 3 directions.

Drama 3 takes intensity words literally: "loud", "raspy", "urgent", "sharp"
or "leaning in" come out as shouting, "whispering" / "low" / "hushed" as a
whisper for the whole segment. The director (Strata) uses them far more often
than the lines call for, so tone_down() keeps them only when the on-screen
line itself asks for that delivery:

    loud / screaming  - narration says shouts, yells, screams, roars, bellows...,
                        or the speech calls for help or for the guards (combat)
    whispering        - narration says whispers, murmurs, mutters, hushes,
                        lowers the voice, under the breath...

Voice texture (raspy, gravelly, guttural, ...) is dropped always: the cloned
reference already carries it, and asking for more makes Drama 3 strain.
Stage business (leaning in, glancing around) is dropped: it is not audible.

Githzerai (Dak'kon excepted, he has his own voice) are austere and
disciplined: their directions become even, restrained and near-monotone.

    >>> tone_down("defensive, loud, aggressive, then suddenly remorseful", "What do yeh think happened?")[0]
    'defensive, confrontational, then suddenly remorseful'
    >>> tone_down("nervous, whispering, glancing around", '"The Dustie calls himself Gravesend..."')[0]
    'nervous'
    >>> tone_down("whispering, fearful", 'He leans in and whispers. "They watch us."')[0]
    'whispering, fearful'
"""

import re
from typing import Dict, List, Tuple

LOUD_CUE_RE = re.compile(
    r"\b(shout|yell|scream|shriek|screech|bellow|roar|howl|cr(?:y|ies|ied) out|bark(?:s|ed)? at|thunder|boom"
    r"|raises? (?:his|her|its|their) voice|at the top of)\w*", re.IGNORECASE)
LOUD_SPEECH_RE = re.compile(r"\b(help|guards?|to arms|attack|kill (?:him|her|them|it))\b", re.IGNORECASE)
WHISPER_CUE_RE = re.compile(
    r"\b(whisper|murmur|mutter|hush|sotto|under (?:his|her|its|their) breath|lowers? (?:his|her|its|their) voice"
    r"|voice drops|breathes|barely audible|softly|quietly|shh+|hss+t)\w*", re.IGNORECASE)

# word -> replacement ("" = drop). Matched against whole comma-separated parts and words inside them.
LOUD_WORDS: Dict[str, str] = {
    "loud": "", "loudly": "", "shouting": "", "shouted": "", "shout": "", "yelling": "", "yell": "",
    "screaming": "", "scream": "", "screamed": "", "shrieking": "", "shrill": "", "bellowing": "", "roaring": "",
    "booming": "", "thundering": "", "howling": "", "raised voice": "", "rising to a shout": "",
    "at full volume": "", "full volume": "", "explosive": "", "spitting words": "contemptuous",
    "spitting": "contemptuous", "frantic": "agitated", "hysterical": "agitated", "panicked": "anxious",
    "furious": "angry", "enraged": "angry", "aggressive": "confrontational", "urgent": "earnest",
    "sharp": "curt", "intense": "focused", "manic": "erratic", "rapid-fire": "quick",
}
WHISPER_WORDS: Dict[str, str] = {
    "whispering": "", "whisper": "", "whispered": "", "whispery": "", "hushed": "", "barely audible": "",
    "under breath": "", "under his breath": "", "under her breath": "", "lowering voice": "",
    "lowered voice": "", "low voice": "", "low volume": "", "low": "", "breathy": "", "broken whisper": "",
    "muffled": "", "lower volume": "", "lowered volume": "", "quiet voice": "", "murmur": "", "murmuring": "",
    "muttering": "", "under the breath": "",
}
TEXTURE_WORDS = {"raspy", "rasping", "gravelly", "gritty", "guttural", "hoarse", "strained", "croaking",
                 "throaty", "nasal", "husky"}
STAGE_WORDS = {"leaning in", "leaning closer", "glancing around", "looking around", "glancing about"}
GITH_PREFIX = "even, restrained, near-monotone"
GITH_KEEP = 2                    # emotion words kept after the githzerai prefix
NEUTRAL_FALLBACK = "matter-of-fact"
WHISPER_TAG_RE = re.compile(r"</?whisper>")


def line_cues(line: str) -> Tuple[bool, bool]:
    """(loud allowed, whisper allowed) from the full on-screen line."""
    quoted = " ".join(re.findall(r'"([^"]*)"', line))
    loud = bool(LOUD_CUE_RE.search(line) or LOUD_SPEECH_RE.search(quoted))
    return loud, bool(WHISPER_CUE_RE.search(line))


def _swap(part: str, table: Dict[str, str]) -> Tuple[str, bool]:
    """Replace a table word (whole part first, then single words) in one comma-separated part."""
    low = part.lower().strip()
    if low in table:
        return table[low], True
    for key in sorted(table, key=len, reverse=True):
        pattern = r"(?<![a-z-])" + re.escape(key) + r"(?![a-z-])"
        if re.search(pattern, low):
            new = re.sub(pattern, table[key], low).strip(" ,")
            return re.sub(r"\s+", " ", new), True
    return part.strip(), False


def tone_down(direction: str, line: str, githzerai: bool = False) -> Tuple[str, List[str]]:
    """
    Make a [direction] (without brackets) fit the line's actual delivery.

    Args:
        direction: The director's direction, e.g. "defensive, loud, aggressive".
        line: The full on-screen line (narration + quotes), for the cues.
        githzerai: Speaker is a githzerai (not Dak'kon).

    Returns:
        (new direction, list of changes like "loud -> (dropped)").
    """
    loud_ok, whisper_ok = line_cues(line)
    changes: List[str] = []
    parts: List[str] = []
    for raw in [p.strip() for p in direction.split(",") if p.strip()]:
        part = raw
        low = part.lower()
        if low in TEXTURE_WORDS or low in STAGE_WORDS:
            changes.append(f"{raw} -> (dropped)")
            continue
        for table, allowed in ((LOUD_WORDS, loud_ok), (WHISPER_WORDS, whisper_ok)):
            if allowed:
                continue
            part, hit = _swap(part, table)
            if hit:
                changes.append(f"{raw} -> {part or '(dropped)'}")
        words = [w for w in part.split() if w.lower() not in TEXTURE_WORDS]
        part = " ".join(words)
        if part and part.lower() not in (p.lower() for p in parts):
            parts.append(part)
    if githzerai:
        kept = [p for p in parts if p.lower() not in GITH_PREFIX][:GITH_KEEP]
        new = ", ".join([GITH_PREFIX] + kept)
        if new != ", ".join(parts):
            changes.append("githzerai: even and restrained")
        return new, changes
    if not parts and changes:
        parts = [NEUTRAL_FALLBACK]
    return ", ".join(parts), changes


def tone_text(text: str, line: str, githzerai: bool = False) -> Tuple[str, List[str]]:
    """
    tone_down() for a full directed text ("[direction] words <whisper>x</whisper>"):
    rewrites the leading direction and removes <whisper> spans the line gives no cue for.
    """
    match = re.match(r"\s*\[([^\[\]]*)\]\s*", text)
    changes: List[str] = []
    body = text[match.end():] if match else text.strip()
    direction = match.group(1) if match else ""
    if direction or githzerai:
        direction, changes = tone_down(direction, line, githzerai)
    if WHISPER_TAG_RE.search(body) and not line_cues(line)[1]:
        body = WHISPER_TAG_RE.sub("", body)
        changes.append("<whisper> removed")
    return (f"[{direction}] {body}" if direction else body), changes
