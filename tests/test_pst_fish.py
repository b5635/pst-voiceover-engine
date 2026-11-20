"""
Unit tests for pst_fish.py: Drama 3 text preparation, director output checks and take checks.
"""

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# pst_fish.py (and pst_revoice.py, which it imports) re-wrap stdout/stderr on win32 at import,
# which breaks pytest's capture
_platform = sys.platform
sys.platform = "linux"
try:
    _spec = importlib.util.spec_from_file_location("pst_fish", Path(__file__).parent.parent / "pst_fish.py")
    assert _spec is not None and _spec.loader is not None
    pst_fish = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(pst_fish)
finally:
    sys.platform = _platform

from libs.tts_drama3 import Take  # noqa: E402

NARRATOR = pst_fish.NARRATOR
PlanRow = pst_fish.PlanRow


def _row(strref: int = 1, speaker: str = "Morte") -> "PlanRow":
    return PlanRow(strref, speaker, "generate", f"VV{strref}", "")


def _take(seconds_loud: float, seconds_quiet: float = 0.0, stop: str = "complete", rate: int = 1000) -> Take:
    """A take: a tone, an optional silent gap, then the tone again."""
    import numpy as np
    tone = (np.sin(np.arange(int(rate * seconds_loud / 2)) * 0.3) * 8000).astype("<i2")
    gap = np.zeros(int(rate * seconds_quiet), dtype="<i2")
    return Take(np.concatenate([tone, gap, tone]).tobytes(), rate, 1, stop)


# ==============================================================================
# collapse_stutters
# ==============================================================================

class TestCollapseStutters:
    def test_stutters_cut_to_one_repeat(self):
        assert pst_fish.collapse_stutters("That s-s-s-sodding b-b-b-bubber! Y-y-y-years, sn-sn-snack, d-d-d-d-days") == \
            "That s-sodding b-bubber! Y-years, sn-snack, d-days"

    def test_syllable_and_mixed_case_stutters(self):
        assert pst_fish.collapse_stutters("muh-muh-memories, Guh-guh-gooooolll, N-N-N-Null, I-I-I-it's") == \
            "muh-memories, Guh-gooooolll, N-Null, I-it's"

    def test_single_repeat_unchanged(self):
        assert pst_fish.collapse_stutters("m-made his leg") == "m-made his leg"

    def test_laughs_and_word_repeats_kept(self):
        for text in ("Hee-hee-heeeeee!", "Hee-Hee-Hee", "tkk-tkk-tkk", "are-are-are", "hedge-wizard"):
            assert pst_fish.collapse_stutters(text) == text

    def test_applied_to_drama3_segments(self):
        segments = pst_fish.drama3_segments('"B-b-b-bastard."', _row(speaker="DWESKULL"), {}, {})
        assert [base for _, base in segments] == ["B-bastard."]


# ==============================================================================
# emphasis_tags
# ==============================================================================

class TestEmphasisTags:
    def test_quoted_word_becomes_emphasis(self):
        assert pst_fish.emphasis_tags('Why don\'t you "try" it') == "Why don't you <emphasis>try</emphasis> it"

    def test_shouted_caps_become_lowercase_emphasis(self):
        assert pst_fish.emphasis_tags("I like this you. A LOT better.") == \
            "I like this you. A <emphasis>lot</emphasis> better."

    def test_single_capital_i_untouched(self):
        assert pst_fish.emphasis_tags("Then I left.") == "Then I left."

    def test_all_caps_text_sentence_cased(self):
        assert pst_fish.emphasis_tags("YOU WILL NOT ESCAPE ME. I SEE YOU") == "You will not escape me. I see you"

    def test_all_caps_with_emphasis_keeps_tags(self):
        assert pst_fish.emphasis_tags('YOU ARE "SPECIAL" TO HIM') == "You are <emphasis>special</emphasis> to him"

    def test_time_budget_counts_breaks_and_drawn_words(self):
        plain = pst_fish.time_budget("one two three four five six")
        hissy = pst_fish.time_budget("Ssso. White. Hornssss. Nasssty. Dead now. Gone.")
        assert hissy > plain

    def test_stray_quote_dropped(self):
        assert pst_fish.emphasis_tags('Odd "quote') == "Odd quote"


# ==============================================================================
# spoken_words / leading_direction
# ==============================================================================

class TestSpokenWords:
    def test_tags_and_directions_ignored(self):
        assert pst_fish.spoken_words("[dry, mocking] Ugly <emphasis>little</emphasis> berks. [chuckles]") == \
            ["ugly", "little", "berks"]

    def test_apostrophes_kept_and_curly_normalised(self):
        assert pst_fish.spoken_words("Ye\u2019re tryin' tae") == ["ye're", "tryin", "tae"]

    def test_leading_direction(self):
        assert pst_fish.leading_direction("  [hushed, fearful] They knew") == "hushed, fearful"
        assert pst_fish.leading_direction("They [sighs] knew") == ""


# ==============================================================================
# sanitize_directed
# ==============================================================================

class TestSanitizeDirected:
    def test_list_wrapped_reply_unwrapped(self):
        assert pst_fish.sanitize_directed("Is that it?", "['mocking, slow pace] Is that it?']") == (
            "[mocking, slow pace] Is that it?", True)
        assert pst_fish.sanitize_directed("Move along.", '["cold] Move along."]') == ("[cold] Move along.", True)
        assert pst_fish.sanitize_directed("2", "[', faint] 2']") == ("[faint] 2", True)

    def test_valid_direction_kept(self):
        assert pst_fish.sanitize_directed("Git!", "[hissing] Git!") == ("[hissing] Git!", True)

    def test_changed_words_fall_back_to_direction_and_base(self):
        assert pst_fish.sanitize_directed("Get!", "[hissing] <forceful>Git</forceful>!") == ("[hissing] Get!", False)

    def test_unknown_tag_dropped(self):
        text, ok = pst_fish.sanitize_directed("You knew.", "[cold] <shout>You</shout> knew.")
        assert ok and text == "[cold] You knew."

    def test_extra_events_trimmed(self):
        text, ok = pst_fish.sanitize_directed("A b c.", "[dir] [sighs] A [laughs] b [gasps] c.")
        assert ok and text == "[dir] [sighs] A [laughs] b c."

    def test_unbalanced_tags_fall_back(self):
        assert pst_fish.sanitize_directed("You knew.", "[cold] <whisper>You knew.") == ("[cold] You knew.", False)

    def test_long_direction_shortened(self):
        text, _ = pst_fish.sanitize_directed("Hi.", "[" + " ".join(["word"] * 30) + "] Hi.")
        assert pst_fish.leading_direction(text).split() == ["word"] * pst_fish.MAX_DIRECTION_WORDS

    def test_no_direction(self):
        assert pst_fish.sanitize_directed("Hi.", "Hi.") == ("Hi.", True)


# ==============================================================================
# directed_segments
# ==============================================================================

class TestDirectedSegments:
    LINE = 'Morte glances at your palm. "Ugly little berks."'

    def test_narrator_gets_fixed_direction_and_speech_the_mood(self):
        segs = pst_fish.directed_segments(_row(), self.LINE, {}, {}, {}, "sarcastic")
        assert [(s["voice"], s["source"]) for s in segs] == [(NARRATOR, "narrator"), ("Morte", "mood")]
        assert segs[0]["text"] == f"[{pst_fish.NARRATOR_DIRECTION}] Morte glances at your palm."
        assert segs[1]["text"] == "[dry, sarcastic] Ugly little berks."

    def test_director_used_when_base_matches(self):
        directions = {1: {"segments": [{"voice": "Morte", "base": "Ugly little berks.",
                                        "text": "[morbidly fascinated] Ugly little berks."}]}}
        segs = pst_fish.directed_segments(_row(), self.LINE, {}, {}, directions, "sarcastic")
        assert segs[1]["text"] == "[morbidly fascinated] Ugly little berks."
        assert segs[1]["source"] == "director"

    def test_stale_director_text_ignored(self):
        directions = {1: {"segments": [{"voice": "Morte", "base": "Old words.", "text": "[sad] Old words."}]}}
        segs = pst_fish.directed_segments(_row(), self.LINE, {}, {}, directions, "neutral")
        assert segs[1] == {"voice": "Morte", "base": "Ugly little berks.", "text": "Ugly little berks.",
                           "source": "mood"}


# ==============================================================================
# take checks
# ==============================================================================

class TestTakeChecks:
    def test_good_take(self):
        assert pst_fish.take_problem(_take(2.0), words=5) == ""

    def test_cut_off(self):
        assert pst_fish.take_problem(_take(2.0, stop="max_seconds"), words=5).startswith("cut off")

    def test_too_short_for_words(self):
        assert pst_fish.take_problem(_take(0.6), words=20).startswith("too short")

    def test_stall_detected(self):
        assert pst_fish.take_problem(_take(2.0, seconds_quiet=3.0), words=5).startswith("stalled")

    def test_trim_removes_edges(self):
        import numpy as np
        rate = 1000
        tone = (np.sin(np.arange(1000) * 0.3) * 8000).astype("<i2")
        pad = np.zeros(1000, dtype="<i2")
        take = Take(np.concatenate([pad, tone, pad]).tobytes(), rate, 1, "complete")
        assert 1.0 <= pst_fish.trim_silence(take).seconds <= 1.25

    def test_voice_key(self):
        assert pst_fish.voice_key("Morte", "sarcastic") == "Morte.sarcastic"
        assert pst_fish.voice_key("Narrator", "-") == "Narrator"
        assert pst_fish.voice_key("nwn:male/old man", "nwn:male/old man") == "nwn_male_old_man"


# ==============================================================================
# snapshot / stale
# ==============================================================================

class TestStaleReasons:
    LINE = '"Ugly little berks."'

    def _row(self):
        return PlanRow(1, "Morte", "generate", "VV1", self.LINE)

    def test_direction_spoken_change(self):
        row = self._row()
        entry = {"speaker": "Morte", "spoken": '"Old."', "voice": "Morte",
                 "segments": [{"voice": "Morte", "base": "Old.", "text": "[sad] Old."}]}
        reasons = pst_fish.direction_stale_reasons(row, row.text, {}, {}, entry)
        assert "spoken text changed" in reasons

    def test_direction_current(self):
        row = self._row()
        bases = [b for v, b in pst_fish.drama3_segments(row.text, row, {}, {}) if v != NARRATOR]
        entry = {"speaker": "Morte", "spoken": row.text, "voice": "Morte",
                 "segments": [{"voice": "Morte", "base": bases[0], "text": f"[dry] {bases[0]}"}]}
        assert pst_fish.direction_stale_reasons(row, row.text, {}, {}, entry) == []

    def test_direction_speaker_change(self):
        row = self._row()
        bases = [b for v, b in pst_fish.drama3_segments(row.text, row, {}, {}) if v != NARRATOR]
        entry = {"speaker": "Annah", "spoken": row.text, "voice": "Morte",
                 "segments": [{"voice": "Morte", "base": bases[0], "text": bases[0]}]}
        assert any("speaker changed" in r for r in pst_fish.direction_stale_reasons(row, row.text, {}, {}, entry))

    def test_direction_voice_only_change_stays_current(self):
        row = PlanRow(1, "Morte", "generate", "VV1", self.LINE, voice="wow:SomeoneElse")
        bases = [b for v, b in pst_fish.drama3_segments(row.text, row, {}, {}) if v != NARRATOR]
        entry = {"speaker": "Morte", "spoken": row.text, "voice": "Morte",
                 "segments": [{"voice": "Morte", "base": bases[0], "text": f"[dry] {bases[0]}"}]}
        assert pst_fish.direction_stale_reasons(row, row.text, {}, {}, entry) == []

    def test_audio_no_record(self):
        row = self._row()
        assert pst_fish.audio_stale_reasons(row, row.text, {}, {}, {}, "", None) == ["no generation.json record"]

    def test_audio_matches_snapshot(self):
        row = self._row()
        directions = {1: {"segments": [{"voice": "Morte", "base": "Ugly little berks.",
                                        "text": "[morbid] Ugly little berks."}]}}
        current = pst_fish.directed_segments(row, row.text, {}, {}, directions, "")
        rec = pst_fish.generation_record(
            row, row.text, {}, {}, Path("directions.json"), directions[1], "", "multi",
            [{"voice": s["voice"], "text": s["text"]} for s in current])
        assert pst_fish.audio_stale_reasons(row, row.text, {}, {}, directions, "", rec) == []

    def test_audio_directed_text_change(self):
        row = self._row()
        directions = {1: {"segments": [{"voice": "Morte", "base": "Ugly little berks.",
                                        "text": "[morbid] Ugly little berks."}]}}
        current = pst_fish.directed_segments(row, row.text, {}, {}, directions, "")
        rec = pst_fish.generation_record(
            row, row.text, {}, {}, Path("directions.json"), directions[1], "", "multi",
            [{"voice": s["voice"], "text": s["text"]} for s in current])
        rec["segments"] = [{"voice": "Morte", "text": "[old] Ugly little berks."}]
        assert "directed text changed" in pst_fish.audio_stale_reasons(row, row.text, {}, {}, directions, "", rec)

    def test_generation_record_has_snapshot(self):
        row = self._row()
        rec = pst_fish.generation_record(row, row.text, {}, {}, Path("d.json"), {}, "", "multi", [])
        assert rec["spoken"] == row.text
        assert rec["voice"] == "Morte"
        assert rec["speaker"] == "Morte"
        assert rec["original"] == ""
        assert "tts" in rec
        assert "rules_fingerprint" in rec
        assert rec["voice_mode"] == "multi"
        assert rec["generated_at"]

    def test_direction_skipped_for_original(self):
        row = PlanRow(1, "Morte", "generate", "VV1", self.LINE, "", "MRT001")
        entry = {"speaker": "Morte", "spoken": '"Old."', "voice": "Morte",
                 "segments": [{"voice": "Morte", "base": "Old.", "text": "[sad] Old."}]}
        assert pst_fish.direction_stale_reasons(row, row.text, {}, {}, entry) == []


class TestKeepOriginal:
    LINE = 'He grins. "Ugly little berks."'

    def test_speech_only_uses_original(self, monkeypatch, tmp_path):
        wav = tmp_path / "MRT001.WAV"
        wav.write_bytes(b"RIFF")
        monkeypatch.setattr(pst_fish, "original_wav", lambda r, dest=None: wav if r else None)
        row = PlanRow(1, "Morte", "generate", "VV1", '"Ugly little berks."', "", "MRT001")
        segs = pst_fish.planned_audio_segments(row, row.text, {}, {}, {}, "")
        assert segs == [{"voice": "Morte", "base": "", "text": "", "source": "original", "resref": "MRT001"}]

    def test_mixed_keeps_original_and_tts_narrator(self, monkeypatch, tmp_path):
        wav = tmp_path / "MRT001.WAV"
        wav.write_bytes(b"RIFF")
        monkeypatch.setattr(pst_fish, "original_wav", lambda r, dest=None: wav if r else None)
        row = PlanRow(1, "Morte", "generate", "VV1", self.LINE, "", "MRT001")
        segs = pst_fish.planned_audio_segments(row, row.text, {}, {}, {}, "")
        assert [s["source"] for s in segs] == ["narrator", "original"]
        assert segs[1]["resref"] == "MRT001"
        assert "He grins" in segs[0]["text"]

    def test_missing_original_falls_back_to_tts(self, monkeypatch):
        monkeypatch.setattr(pst_fish, "original_wav", lambda r, dest=None: None)
        row = PlanRow(1, "Morte", "generate", "VV1", '"Hi."', "", "MRT001")
        segs = pst_fish.planned_audio_segments(row, row.text, {}, {}, {}, "")
        assert segs[0]["source"] == "mood"
        assert segs[0]["voice"] == "Morte"

    def test_audio_stale_when_switching_to_original(self, monkeypatch, tmp_path):
        wav = tmp_path / "MRT001.WAV"
        wav.write_bytes(b"RIFF")
        monkeypatch.setattr(pst_fish, "original_wav", lambda r, dest=None: wav if r else None)
        row = PlanRow(1, "Morte", "generate", "VV1", '"Ugly little berks."', "", "MRT001")
        rec = pst_fish.generation_record(
            row, row.text, {}, {}, Path("d.json"), {}, "", "multi",
            [{"voice": "Morte", "text": "Ugly little berks.", "source": "mood"}])
        assert "now keeps original audio" in pst_fish.audio_stale_reasons(
            row, row.text, {}, {}, {}, "", rec)

    def test_audio_current_when_keeping_original(self, monkeypatch, tmp_path):
        wav = tmp_path / "MRT001.WAV"
        wav.write_bytes(b"RIFF")
        monkeypatch.setattr(pst_fish, "original_wav", lambda r, dest=None: wav if r else None)
        row = PlanRow(1, "Morte", "generate", "VV1", '"Ugly little berks."', "", "MRT001")
        segs = pst_fish.planned_audio_segments(row, row.text, {}, {}, {}, "")
        rec = pst_fish.generation_record(row, row.text, {}, {}, Path("d.json"), {}, "", "multi", segs)
        assert pst_fish.audio_stale_reasons(row, row.text, {}, {}, {}, "", rec) == []
