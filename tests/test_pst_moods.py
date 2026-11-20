"""
Unit tests for the mood pipeline: libs/dlg_tree.py, pst_moods.py and pst_classify.py.
"""

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.dlg_tree import history, load_tree  # noqa: E402


def load_script(filename: str, module_name: str):
    """Import a script without its win32 stdout reconfigure tripping pytest's capture."""
    platform = sys.platform
    sys.platform = "linux"
    try:
        spec = importlib.util.spec_from_file_location(module_name, Path(__file__).parent.parent / filename)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.platform = platform


pst_moods = load_script("pst_moods.py", "pst_moods")
pst_classify = load_script("pst_classify.py", "pst_classify")


# ==============================================================================
# DLG tree
# ==============================================================================


def make_dlg(states, transitions) -> bytes:
    """
    states: [(say_strref, first_trans, n_trans)]
    transitions: [(flags, text_strref, next_dlg, next_state)]
    """
    header_size = 0x34
    state_off = header_size
    trans_off = state_off + 16 * len(states)
    data = bytearray(b"DLG V1.0")
    data += struct.pack("<IIII", len(states), state_off, len(transitions), trans_off)
    data += bytes(header_size - len(data))
    for say, first, count in states:
        data += struct.pack("<iIIi", say, first, count, -1)
    for flags, text, next_dlg, next_state in transitions:
        data += struct.pack("<Iiiii", flags, text, -1, -1, -1)
        data += next_dlg.encode("ascii").ljust(8, b"\x00")
        data += struct.pack("<I", next_state)
    return bytes(data)


@pytest.fixture
def dlg_dir(tmp_path):
    # DMORTE: state0 (100) -player 200-> state1 (101) -player 201-> DANNAH state0 (300)
    (tmp_path / "DMORTE.DLG").write_bytes(make_dlg(
        [(100, 0, 1), (101, 1, 1)],
        [(1, 200, "DMORTE", 1), (1, 201, "DANNAH", 0)],
    ))
    # DANNAH: state0 (300) -no text-> end
    (tmp_path / "DANNAH.DLG").write_bytes(make_dlg([(300, 0, 1)], [(0, -1, "", 0)]))
    return tmp_path


def test_history_walks_back_across_dialogs(dlg_dir):
    tree = load_tree(dlg_dir)
    assert history(tree, 300, 4) == [("npc", 100), ("player", 200), ("npc", 101), ("player", 201)]


def test_history_depth_limit(dlg_dir):
    tree = load_tree(dlg_dir)
    assert history(tree, 300, 1) == [("npc", 101), ("player", 201)]


def test_history_unknown_or_root_line_is_empty(dlg_dir):
    tree = load_tree(dlg_dir)
    assert history(tree, 999, 4) == []
    assert history(tree, 100, 4) == []


# ==============================================================================
# pst_moods
# ==============================================================================


def test_pick_clips_longest_first_within_limits():
    picked = pst_moods.pick_clips([("A", 6.0), ("B", 3.0), ("C", 1.5), ("D", 2.5)])
    assert picked == [("A", 6.0), ("B", 3.0)]


def test_pick_clips_skips_overshoot_and_too_short():
    assert pst_moods.pick_clips([("A", 9.0), ("B", 3.0)]) == [("A", 9.0)]
    assert pst_moods.pick_clips([("A", 1.9), ("B", 1.0)]) == []


def test_pick_clips_needs_min_seconds():
    assert pst_moods.pick_clips([("A", 2.5), ("B", 2.0)]) == []


def clip(speaker, resref, duration):
    return {"Speaker": speaker, "SoundResRef": resref, "Duration": str(duration)}


def test_group_and_manifest_default_and_dropped_moods():
    clips = [clip("Annah", "A1", 6.0), clip("Annah", "A2", 3.0), clip("Annah", "A3", 3.0),
             clip("Annah", "A4", 2.0)]
    labels = {"Annah": {"A1": "angry", "A2": "angry", "A3": "warm", "A4": "skip"}}
    groups = pst_moods.group_by_mood(clips, labels)
    manifest = pst_moods.build_manifest(groups, {c["SoundResRef"]: c["SoundResRef"] for c in clips})
    annah = manifest["Annah"]
    assert set(annah["moods"]) == {"angry"}           # warm (3s) is too thin
    assert annah["default"] == "angry"                # no neutral, so the biggest mood
    assert annah["moods"]["angry"]["clips"] == ["A1", "A2"]


def test_manifest_pools_thin_moods_into_one_reference():
    clips = [clip("Dak", "D1", 3.0), clip("Dak", "D2", 3.0)]
    labels = {"Dak": {"D1": "solemn", "D2": "angry"}}
    manifest = pst_moods.build_manifest(pst_moods.group_by_mood(clips, labels), {"D1": "a", "D2": "b"})
    assert manifest["Dak"]["default"] == "neutral"
    assert manifest["Dak"]["moods"]["neutral"]["clips"] == ["D1", "D2"]


def test_unknown_mood_label_rejected():
    with pytest.raises(ValueError):
        pst_moods.group_by_mood([clip("Annah", "A1", 6.0)], {"Annah": {"A1": "grumpy"}})


# ==============================================================================
# pst_classify
# ==============================================================================

MOODS = {"angry": {"description": "hostile"}, "warm": {"description": "gentle"}}


def test_parse_mood_json():
    assert pst_classify.parse_mood('{"mood": "Warm", "reason": "tender"}', MOODS, "angry") == ("warm", "tender")


def test_parse_mood_ignores_thinking_and_extra_text():
    reply = '<think>maybe angry?</think>Sure: {"mood": "warm", "reason": "soft"} done'
    assert pst_classify.parse_mood(reply, MOODS, "angry")[0] == "warm"


def test_parse_mood_free_text_and_fallback():
    assert pst_classify.parse_mood("I'd say angry.", MOODS, "warm")[0] == "angry"
    assert pst_classify.parse_mood('{"mood": "sad"}', MOODS, "warm") == ("warm", "fallback: no valid mood in reply")


def test_build_prompt_includes_moods_and_context():
    prompt = pst_classify.build_prompt("Annah", '"Pike off."', MOODS, [("Player (The Nameless One)", '"Hi."')])
    assert "- angry: hostile" in prompt
    assert 'Player (The Nameless One): "Hi."' in prompt
    assert prompt.endswith('"Pike off."')
