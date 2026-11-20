"""
Conversation-tree reader for Infinity Engine DLG V1.0 files.

Builds, across every DLG in a folder, a reverse index from each NPC line
(state) to the player replies (transitions) that lead to it, so callers can
walk back up the conversation: "what was said before this line, and which
choice did the player pick to get here?"
"""

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from libs.utils import iter_files_ci

StateKey = Tuple[str, int]   # (DLG resref, state index)

_TRANS_HAS_TEXT = 0x01


@dataclass
class Parent:
    """
    A way into a state: from `state` the player picked `reply_strref`.

    Attributes:
        state: The state the player was answering.
        reply_strref: The player's reply text, or -1 if the transition has no text.
    """
    state: StateKey
    reply_strref: int


@dataclass
class DlgTree:
    """
    Every state's SAY strref plus the reverse transition index.

    Attributes:
        say: state -> NPC line strref.
        parents: state -> transitions that lead to it.
        state_of: NPC line strref -> first state that says it.
    """
    say: Dict[StateKey, int]
    parents: Dict[StateKey, List[Parent]]
    state_of: Dict[int, StateKey]


def _parse(path: Path) -> Optional[Tuple[List[int], List[Tuple[int, int, int, str, int]]]]:
    """
    Read one DLG's states and transitions.

    Returns:
        (SAY strref per state, [(source_state, flags, text_strref, next_dlg, next_state)]),
        or None if the file isn't a DLG V1.0.
    """
    data = path.read_bytes()
    if len(data) < 0x30 or data[0:4] != b"DLG " or data[4:8] not in (b"V1.0", b"V1  "):
        return None
    num_states, state_off, num_trans, trans_off = struct.unpack_from("<IIII", data, 0x0008)

    says: List[int] = []
    owner: Dict[int, int] = {}   # transition index -> state index
    for i in range(num_states):
        rec = state_off + i * 16
        if rec + 12 > len(data):
            break
        strref, first, count = struct.unpack_from("<iII", data, rec)
        says.append(strref)
        for t in range(first, first + count):
            owner.setdefault(t, i)

    transitions = []
    for t in range(num_trans):
        rec = trans_off + t * 32
        if rec + 32 > len(data) or t not in owner:
            continue
        flags, text = struct.unpack_from("<Ii", data, rec)
        next_dlg = data[rec + 0x14:rec + 0x1C].split(b"\x00", 1)[0].decode("ascii", errors="replace").upper()
        (next_state,) = struct.unpack_from("<I", data, rec + 0x1C)
        transitions.append((owner[t], flags, text, next_dlg, next_state))
    return says, transitions


def load_tree(dlg_dir: Path) -> DlgTree:
    """
    Parse every DLG in a folder into one cross-file conversation tree.

    Args:
        dlg_dir: Folder of extracted .DLG files.

    Returns:
        The combined tree.
    """
    say: Dict[StateKey, int] = {}
    parents: Dict[StateKey, List[Parent]] = {}
    state_of: Dict[int, StateKey] = {}

    for path in sorted(iter_files_ci(dlg_dir, "dlg")):
        parsed = _parse(path)
        if parsed is None:
            continue
        dlg = path.stem.upper()
        says, transitions = parsed
        for i, strref in enumerate(says):
            say[(dlg, i)] = strref
            if strref >= 0:
                state_of.setdefault(strref, (dlg, i))
        for src, flags, text, next_dlg, next_state in transitions:
            if not next_dlg:
                continue   # conversation ends here
            reply = text if flags & _TRANS_HAS_TEXT else -1
            parents.setdefault((next_dlg, next_state), []).append(Parent((dlg, src), reply))

    return DlgTree(say, parents, state_of)


def history(tree: DlgTree, strref: int, depth: int) -> List[Tuple[str, int]]:
    """
    Walk back up the conversation from an NPC line.

    Follows the first way into each state (a line can be reached several ways;
    the first is a representative path) for up to `depth` exchanges.

    Args:
        tree: From load_tree().
        strref: The NPC line to start from.
        depth: Number of (NPC line, player reply) exchanges to collect.

    Returns:
        Chronological [(role, strref)] with role "npc" or "player", ending with the
        player reply that leads to `strref`. Empty if the line isn't in any DLG.
    """
    state = tree.state_of.get(strref)
    steps: List[Tuple[str, int]] = []
    seen = {state}
    for _ in range(depth):
        ways = tree.parents.get(state) if state else None
        if not ways:
            break
        parent = ways[0]
        if parent.reply_strref >= 0:
            steps.append(("player", parent.reply_strref))
        prev_say = tree.say.get(parent.state, -1)
        if prev_say >= 0:
            steps.append(("npc", prev_say))
        if parent.state in seen:
            break
        seen.add(parent.state)
        state = parent.state
    return list(reversed(steps))
