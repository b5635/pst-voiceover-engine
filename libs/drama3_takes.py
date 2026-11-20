"""
Drama 3 takes: speak one directed text with retries and quality checks.

Shared by pst_fish.py (this PC) and pst_farm_worker.py (farm PCs), so a take
spoken on another PC goes through exactly the same seeds, time budget,
chunking and runaway / truncation guards. Needs only numpy and
libs/tts_drama3.py, so the farm worker can run without the rest of the repo.
"""

import logging
import re
import zlib
from typing import List, Tuple

from libs.tts_drama3 import Drama3Worker, Take

logger = logging.getLogger(__name__)

# ==================== CONFIGURATION ====================
MAX_DIRECTION_WORDS = 20
ATTEMPTS = 3                # takes per segment before keeping the best bad one
SECONDS_PER_WORD = 0.7      # max_seconds budget: 3 s + this per word (runaway-silence guard)
SECONDS_PER_BREAK = 0.5     # + this per sentence break / drawn-out word (time_budget)
CUTOFF_GROWTH = 1.5         # a take cut off at max_seconds retries with this much more time
MAX_BUDGET = 47.6           # longest single take (seconds)
CHUNK_WORDS = 70            # longer segments are spoken in sentence chunks (chunk_text)
CHUNK_GAP = 0.25            # seconds of silence between chunks
CHUNK_SEED_STRIDE = 1000    # chunk seeds: index * stride + chunk
MIN_SECONDS_PER_WORD = 0.12 # shorter than this means words were dropped
MAX_INNER_SILENCE = 2.0     # a pause longer than this inside a take means it stalled
SILENCE_DB = -45.0          # trim threshold for leading/trailing silence
# =======================================================

WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")
BRACKET_RE = re.compile(r"\[[^\[\]]*\]")
PAIRED_RE = re.compile(r"</?([a-z_]+)>")


# ==================== TEXT ====================

def spoken_words(text: str) -> List[str]:
    """The words a Drama 3 text speaks: tags and directions removed, lowercased."""
    text = PAIRED_RE.sub(" ", BRACKET_RE.sub(" ", text))
    return WORD_RE.findall(text.lower().replace("’", "'"))


def leading_direction(text: str) -> str:
    """The [direction] a text starts with, without brackets, or ""."""
    match = re.match(r"\s*\[([^\[\]]*)\]", text)
    return match.group(1).strip() if match else ""


def _balanced(text: str) -> bool:
    stack: List[str] = []
    for match in PAIRED_RE.finditer(text):
        tag, closing = match.group(1), match.group(0).startswith("</")
        if not closing:
            stack.append(tag)
        elif not stack or stack.pop() != tag:
            return False
    return not stack


def with_direction(direction: str, text: str) -> str:
    """Prefix a [direction] (if any) to a text."""
    direction = " ".join(direction.split()[:MAX_DIRECTION_WORDS]).strip(" ,.")
    return f"[{direction}] {text}" if direction else text


# ==================== AUDIO CHECKS ====================

def _levels(take: Take, frame_ms: int = 20):
    import numpy as np
    samples = np.frombuffer(take.pcm, dtype="<i2").astype(np.float32) / 32768.0
    frame = max(1, take.sample_rate * frame_ms // 1000)
    n = len(samples) // frame
    if n == 0:
        return samples, frame, np.zeros(0)
    rms = np.sqrt(np.mean(samples[: n * frame].reshape(n, frame) ** 2, axis=1) + 1e-12)
    return samples, frame, 20 * np.log10(rms)


def trim_silence(take: Take, pad_ms: int = 80) -> Take:
    """Cut leading/trailing silence (below SILENCE_DB), keeping a little padding."""
    samples, frame, db = _levels(take)
    loud = (db > SILENCE_DB).nonzero()[0]
    if len(loud) == 0:
        return take
    pad = take.sample_rate * pad_ms // 1000
    start = max(0, loud[0] * frame - pad)
    end = min(len(samples), (loud[-1] + 1) * frame + pad)
    return Take(take.pcm[start * 2:end * 2], take.sample_rate, take.seed, take.stop)


def longest_inner_silence(take: Take) -> float:
    """Longest run of silence (seconds) between the first and last sound."""
    _, frame, db = _levels(take)
    loud = (db > SILENCE_DB).nonzero()[0]
    if len(loud) < 2:
        return 0.0
    gaps = loud[1:] - loud[:-1]
    return float(gaps.max() - 1) * frame / take.sample_rate


def take_problem(take: Take, words: int) -> str:
    """Why a (trimmed) take is unusable, or "" if it looks fine."""
    if take.stop != "complete":
        return f"cut off ({take.stop})"
    if take.seconds < max(0.4, MIN_SECONDS_PER_WORD * words):
        return f"too short ({take.seconds:.1f}s for {words} words)"
    gap = longest_inner_silence(take)
    if gap > MAX_INNER_SILENCE:
        return f"stalled ({gap:.1f}s silence)"
    return ""


# ==================== SPEAKING ====================

def seed_for(strref: int, index: int, attempt: int) -> int:
    """Reproducible seed per segment and attempt."""
    return zlib.crc32(f"{strref}:{index}:{attempt}".encode()) & 0x7FFFFFFF


def time_budget(text: str) -> float:
    """
    max_seconds for a take: 3 s + SECONDS_PER_WORD per word, plus a pause per
    sentence break and per drawn-out word ("Hornssss", "guuuurk"), which
    Drama 3 acts out slowly. "It'sss a trocopotoca. White. Hornssss. Nasssty."
    used to be cut off three times at the old words-only budget.
    """
    words = spoken_words(text)
    breaks = len(re.findall(r"[.!?]+(?=\s|$)|\.\.\.|\s[-—]\s", PAIRED_RE.sub(" ", BRACKET_RE.sub(" ", text))))
    drawn = sum(1 for w in words if re.search(r"([a-z])\1\1", w))
    return min(MAX_BUDGET, 3.0 + SECONDS_PER_WORD * len(words) + SECONDS_PER_BREAK * breaks
               + SECONDS_PER_BREAK * drawn)


def chunk_text(text: str, max_words: int = CHUNK_WORDS) -> List[str]:
    """
    Split a long directed text at sentence ends into pieces of at most
    `max_words` (a longer single sentence stays whole), each with the same
    leading [direction]. One Drama 3 take is capped at MAX_BUDGET seconds
    (~100 words at the ~0.45 s/word Drama 3 reads at), and the longest PST
    descriptions run to 170 words. A split that would break a <tag> span is skipped.
    """
    direction = leading_direction(text)
    body = re.sub(r"^\s*\[[^\[\]]*\]\s*", "", text)
    if len(spoken_words(body)) <= max_words:
        return [text]
    chunks: List[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?])\s+(?=\S)", body):
        joined = f"{current} {sentence}".strip()
        if current and len(spoken_words(joined)) > max_words and _balanced(current):
            chunks.append(current)
            current = sentence
        else:
            current = joined
    if current:
        chunks.append(current)
    return [with_direction(direction, c) for c in chunks]


def speak_once(worker: Drama3Worker, strref: int, voice: str, text: str, seed_index: int,
               label: str = "") -> Tuple[Take, List[str]]:
    """Retry runaway or truncated takes of one text; the first good (or the last) take."""
    words = len(spoken_words(text))
    budget = time_budget(text)
    attempts: List[str] = []
    take = None
    for attempt in range(ATTEMPTS):
        take = trim_silence(worker.synthesize(text, voice, seed=seed_for(strref, seed_index, attempt),
                                              max_seconds=budget))
        problem = take_problem(take, words)
        attempts.append(problem or "ok")
        if not problem:
            break
        if take.stop == "max_seconds":   # a slow, pause-heavy read: give the next take more room
            budget = min(MAX_BUDGET, budget * CUTOFF_GROWTH)
        logger.info(f"   ↻ {label or strref} attempt {attempt + 1}: {problem}")
    return take, attempts


def speak_segment(worker: Drama3Worker, strref: int, index: int, voice: str, text: str) -> Tuple[Take, List[str]]:
    """
    Speak one segment; long texts are spoken in sentence chunks (chunk_text)
    joined with a short pause. The attempts list ends with the worst chunk's
    final result ("ok" when every chunk was fine).

    Args:
        worker: Running Drama 3 worker with `voice` added.
        strref, index: Line and segment number (they pick the seeds).
        voice: Worker voice name.
        text: Directed Drama 3 text.
    """
    label = f"{strref}#{index}"
    chunks = chunk_text(text)
    if len(chunks) == 1:
        return speak_once(worker, strref, voice, text, index, label)
    pieces, results, attempts = [], [], []
    for i, chunk in enumerate(chunks):
        take, chunk_attempts = speak_once(worker, strref, voice, chunk, index * CHUNK_SEED_STRIDE + i, label)
        attempts += [f"chunk {i + 1}: {a}" for a in chunk_attempts]
        results.append(chunk_attempts[-1])
        pieces.append(take)
    gap = b"\x00\x00" * int(pieces[0].sample_rate * CHUNK_GAP)
    pcm = gap.join(t.pcm for t in pieces)
    stop = next((t.stop for t in pieces if t.stop != "complete"), "complete")
    attempts.append(next((r for r in results if r != "ok"), "ok"))
    return Take(pcm, pieces[0].sample_rate, pieces[0].seed, stop), attempts
