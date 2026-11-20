"""
Unit tests for pst_direct.py: which lines get directed, and that Strata is not
fed the OmniVoice mood list.
"""

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_platform = sys.platform
sys.platform = "linux"
try:
    _spec = importlib.util.spec_from_file_location("pst_direct", Path(__file__).parent.parent / "pst_direct.py")
    assert _spec is not None and _spec.loader is not None
    pst_direct = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(pst_direct)
finally:
    sys.platform = _platform

PlanRow = pst_direct.PlanRow


def _row(strref: int, speaker: str, voice: str = "") -> "PlanRow":
    return PlanRow(strref, speaker, "generate", f"VV{strref}", '"Hello."', voice)


class TestLinesToDirect:
    PLAN = [
        _row(1, "Morte"),
        _row(2, "Creeden", "wow:goblinmalezanynpc"),
        _row(3, "Annah"),
        PlanRow(4, "Morte", "skip", "VV4", "unused"),
    ]

    def test_includes_cloned_and_library_npcs(self):
        todo = pst_direct.lines_to_direct(self.PLAN)
        assert [p.strref for p in todo] == [1, 2, 3]

    def test_skips_already_directed_unless_force(self):
        assert [p.strref for p in pst_direct.lines_to_direct(self.PLAN, already={1, 2})] == [3]
        assert [p.strref for p in pst_direct.lines_to_direct(self.PLAN, already={1, 2}, force=True)] == [1, 2, 3]

    def test_includes_stale_directed_lines(self):
        assert [p.strref for p in pst_direct.lines_to_direct(
            self.PLAN, already={1, 2}, stale={2})] == [2, 3]

    def test_speaker_and_strref_filters(self):
        assert [p.strref for p in pst_direct.lines_to_direct(self.PLAN, speakers=["Creeden"])] == [2]
        assert [p.strref for p in pst_direct.lines_to_direct(self.PLAN, strrefs=[3, 1])] == [3, 1]

    def test_skips_originally_voiced_lines(self):
        plan = [
            PlanRow(1, "Morte", "generate", "VV1", '"Hello."', "", "MRT001"),
            _row(2, "Creeden", "wow:goblinmalezanynpc"),
        ]
        assert [p.strref for p in pst_direct.lines_to_direct(plan)] == [2]


class TestBuildPrompt:
    SEGMENTS = [
        {"i": 0, "voice": "Narrator", "base": "He grins."},
        {"i": 1, "voice": "Creeden", "base": "Heh-hooo!"},
    ]

    def test_no_omnivoice_mood_menu(self):
        prompt = pst_direct.build_prompt(
            "Creeden", "Greedy Hive tout.",
            '"Heh-hooo!"', [("Player (The Nameless One)", '"Tell me about Sigil."')], self.SEGMENTS)
        assert "Pick the recorded clip" not in prompt
        assert "Recorded voice clip mood" not in prompt
        assert "one of:" not in prompt
        assert "sarcastic" not in prompt
        assert "Character profile:\nGreedy Hive tout." in prompt
        assert "Voice clone:" not in prompt
        assert "1 (speech): Heh-hooo!" in prompt

    def test_prompt_never_names_the_voice_clone(self):
        prompt = pst_direct.build_prompt("Morte", "Floating talking skull.", '"Pike off."', [], [
            {"i": 0, "voice": "Morte", "base": "Pike off."}])
        assert "Voice clone:" not in prompt
        assert "Character profile:\nFloating talking skull." in prompt


class TestCastProfile:
    def test_joins_personality_voice_appearance(self):
        text = pst_direct.format_cast_profile({
            "profile": {
                "personality": "Greedy and theatrical.",
                "voice": "Low and gritty.",
                "appearance": "A Hive thug.",
            }
        })
        assert text == "Greedy and theatrical. Low and gritty. A Hive thug."

    def test_empty_profile(self):
        assert pst_direct.format_cast_profile({}) == ""
