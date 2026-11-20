"""Tests for the review queue, delivery guard, quote splitter fixes and cast rules."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pst_cast  # noqa: E402
import pst_review  # noqa: E402
from libs.pst_tone import tone_down, tone_text  # noqa: E402
from pst_revoice import NARRATOR, convert_emotes, split_segments  # noqa: E402


class TestSplitSegments:
    def test_inscription_number_stays_with_narrator(self):
        text = 'The number on his forehead reads "1664". This corpse is a librarian.'
        assert split_segments(text, "Z") == [(NARRATOR, text)]

    def test_quoted_word_inside_narration(self):
        text = 'The word "REPENT" has been engraved; a rougher hand chiseled "996" onto its temples.'
        assert [v for v, _ in split_segments(text, "Z")] == [NARRATOR]

    def test_read_out_block_is_narrated(self):
        text = 'The calligraphy reads:\n\n"this may not KILL you"\n"STOP CHASING ME"\n"now... DIE"'
        assert [v for v, _ in split_segments(text, "Z")] == [NARRATOR]

    def test_doubled_quote_typo(self):
        segs = split_segments('""Huh? G-guuurk!" He retches.', "Drunk")
        assert segs == [("Drunk", "Huh? G-guuurk!"), (NARRATOR, "He retches.")]

    def test_real_speech_unchanged(self):
        assert split_segments('"Wait," he says. "Hold on."', "X") == [
            ("X", "Wait,"), (NARRATOR, "he says."), ("X", "Hold on.")]
        assert split_segments('He looks at you. "Fine." Then he leaves.', "X")[1] == ("X", "Fine.")

    def test_player_words_go_to_narrator(self):
        segs = split_segments('"Takes a while." Before replying, you mutter, "Tell me about it."', "N")
        assert segs[-1][0] == NARRATOR

    def test_emotes_become_sounds(self):
        assert convert_emotes([("G", "::Growl::")], "G") == [("G", "Grrrrrr...")]
        assert convert_emotes([(NARRATOR, "::Yawn::"), ("G", "Nap time.")], "G") == [("G", "Aaaahhh... hmm. Nap time.")]
        assert convert_emotes([("G", "::Looks Confused::")], "G") == []


class TestTone:
    def test_loud_dropped_without_cue(self):
        new, changes = tone_down("defensive, loud, aggressive, then suddenly remorseful", '"What happened?"')
        assert "loud" not in new and "aggressive" not in new and changes

    def test_loud_kept_when_calling_for_help(self):
        assert tone_down("terrified, screaming", '"Help! Help me!"')[0] == "terrified, screaming"

    def test_whisper_needs_cue(self):
        assert tone_down("nervous, whispering", '"The Dustie..."')[0] == "nervous"
        assert tone_down("nervous, whispering", 'She lowers her voice. "The Dustie..."')[0] == "nervous, whispering"

    def test_texture_and_stage_dropped(self):
        assert tone_down("smug, raspy, leaning in", '"x"')[0] == "smug"

    def test_githzerai_even(self):
        assert tone_down("furious, loud", '"x"', githzerai=True)[0].startswith("even, restrained")

    def test_whisper_tag_removed(self):
        assert tone_text("[calm] a <whisper>secret</whisper>", '"a secret"')[0] == "[calm] a secret"


class TestCastRules:
    voices = [
        {"id": "nwn:Golem_Good", "gender": "male", "race": "Golem"},
        {"id": "nwn:Deekin_XP2", "gender": "male", "race": "Kobold"},
        {"id": "nwn:ChildM1", "gender": "male", "race": "Human"},
        {"id": "wow:orcmaleguardnpc", "gender": "male", "race": "Orc"},
        {"id": "nwn:Human1", "gender": "male", "race": "Human"},
        {"id": "nwn:ZeliepM", "gender": "male", "race": "Spirit"},
    ]
    rules = {
        "exclude_voices": ["nwn:ChildM1"],
        "force_voices": [{"when": ["modron"], "voices": ["nwn:Golem_Good"]},
                         {"when": ["wererat"], "voices": ["nwn:Deekin_XP2"]},
                         {"when": ["spirit"], "gender": "male", "voices": ["nwn:ZeliepM"]}],
        "only_for": {"nwn:Deekin_XP2": ["wererat"], "wow:orcmaleguardnpc": ["orc"]},
        "avoid_races": [{"when": ["githyanki"], "races": ["Orc"]}],
    }

    def pool(self, npc, profile):
        cands = [v for v in self.voices if v["gender"] == "male"]
        return [v["id"] for v in pst_cast.apply_rules(cands, npc, profile, self.voices, self.rules)]

    def test_modron_forced(self):
        assert self.pool({"speaker": "Modron"}, {"type": "modron"}) == ["nwn:Golem_Good"]

    def test_wererat_forced_and_deekin_banned_elsewhere(self):
        assert self.pool({"speaker": "Wererat"}, {"type": "wererat"}) == ["nwn:Deekin_XP2"]
        assert "nwn:Deekin_XP2" not in self.pool({"speaker": "Phineas"}, {"type": "human bureaucrat"})

    def test_children_and_orcs_excluded(self):
        pool = self.pool({"speaker": "Thug"}, {"type": "human thug"})
        assert "nwn:ChildM1" not in pool and "wow:orcmaleguardnpc" not in pool

    def test_spirit_from_narration(self):
        npc = {"speaker": "", "lines": [{"text": "The spirit sings."}] * 3}
        assert self.pool(npc, {"type": "undead corpse"}) == ["nwn:ZeliepM"]

    def test_word_start_matching(self):
        assert pst_cast.term_match(["ghost"], "ghostly echo")
        assert not pst_cast.term_match(["rat"], "pirate")


class TestReviewFlags:
    def codes(self, text, speaker="X"):
        return {f.code for f in pst_review.text_flags(text, speaker)}

    def test_quote_typos(self):
        assert "quotes" in self.codes('""Huh?" He retches."')
        assert "quotes" not in self.codes('"Fine." He nods.')

    def test_dev_text(self):
        assert "dev-text" in self.codes('"I chant outside the kiosk in AR0200 (Hive B)."')

    def test_needs_review_and_mark(self):
        flags = [pst_review.Flag("quotes", "x"), pst_review.Flag("toned", "y")]
        assert pst_review.needs_review(flags, None)
        marks = {}
        pst_review.mark_reviewed(marks, 1, flags)
        assert not pst_review.needs_review(flags, marks[1])
        assert pst_review.needs_review(flags + [pst_review.Flag("take", "z")], marks[1])

    def test_info_only_not_queued(self):
        assert not pst_review.needs_review([pst_review.Flag("toned", "y")], None)

    def test_audio_flags_cut_off(self):
        rec = {"segments": [{"text": "a b c", "attempts": ["cut off (max_seconds)"] * 3}]}
        assert [f.code for f in pst_review.audio_flags(rec, 2.0)] == ["take"]


class TestRecastStatus:
    def test_next_action_asks_for_cast_when_rules_broken(self):
        import pst_pipeline
        status = pst_pipeline.PipelineStatus(lines=10, directed=10, generated=10, strata="loaded",
                                              drama3="ready", recast=3,
                                              need_direct=0, need_audio=0, wow_missing=0)
        key, message = pst_pipeline.next_action(status)
        assert key == "cast" and "3" in message


class TestStarSpeech:
    def test_vhailor_asterisk_speech(self):
        segs = split_segments("Vhailor falls still. *I will HEAR of your punishment.*", "Vhailor")
        assert segs == [(NARRATOR, "Vhailor falls still."), ("Vhailor", "I will HEAR of your punishment.")]

    def test_single_word_emphasis_stays_narration(self):
        assert [v for v, _ in split_segments("You *do* possess the years.", "X")] == [NARRATOR]


class TestPassTwo:
    def test_ui_note_not_voiced(self):
        segs = split_segments('"Go - Go!"\n\nNOTE: You have been assigned a quest.', "Ei-Vene")
        assert segs == [("Ei-Vene", "Go - Go!")]

    def test_star_emote_sounds(self):
        from pst_revoice import emote_sound
        assert emote_sound("*Cough* - *Cough*") == "Ahem! Hem. Ahem! Hem."
        assert emote_sound("*Hic*") == "Hic!"

    def test_emphasis_tidy(self):
        from pst_fish import tidy_emphasis
        assert (tidy_emphasis("<emphasis>we</emphasis>'<emphasis>re</emphasis> dead")
                == "<emphasis>we're</emphasis> dead")
        assert tidy_emphasis("<emphasis><emphasis>x</emphasis> y</emphasis> z") == "<emphasis>x y</emphasis> z"
        assert (tidy_emphasis("<emphasis>All of <emphasis>it</emphasis>.</emphasis>")
                == "All of <emphasis>it</emphasis>.")

    def test_long_text_chunked(self):
        from pst_fish import chunk_text, spoken_words
        text = "[calm] " + " ".join(f"Sentence {i} has exactly seven words here." for i in range(30))
        chunks = chunk_text(text)
        assert len(chunks) > 1 and all(c.startswith("[calm] ") for c in chunks)
        assert all(len(spoken_words(c)) <= 70 for c in chunks)

    def test_wow_race_only_for_fitting_npcs(self):
        voices = [{"id": "wow:goblinvendorm", "gender": "male", "race": "Goblin"},
                  {"id": "nwn:Human1", "gender": "male", "race": "Human"}]
        rules = {"only_for_races": {"wow:Goblin": ["goblin"]}}
        human = pst_cast.apply_rules(voices, {"speaker": "Merchant"}, {"type": "human merchant"}, voices, rules)
        assert [v["id"] for v in human] == ["nwn:Human1"]
        gob = pst_cast.apply_rules(voices, {"speaker": "X"}, {"type": "goblin trader"}, voices, rules)
        assert len(gob) == 2


class TestPromptVersion:
    def test_old_strata_direction_is_stale_manual_is_not(self):
        import pst_fish
        from pst_revoice import PlanRow
        row = PlanRow(1, "Pharod", "generate", "VV1", '"Hello there."', voice="nwn:x")
        bases = [b for v, b in pst_fish.drama3_segments(row.text, row, {}, {})]
        seg = [{"voice": "nwn:x", "base": bases[0], "text": f"[calm] {bases[0]}"}]
        old = {"source": "strata:qwen", "speaker": "Pharod", "spoken": row.text, "segments": seg}
        assert "older director prompt" in " ".join(pst_fish.direction_stale_reasons(row, row.text, {}, {}, old))
        new = {**old, "prompt_version": pst_fish.DIRECTOR_PROMPT_VERSION}
        assert pst_fish.direction_stale_reasons(row, row.text, {}, {}, new) == []
        manual = {**old, "source": "manual"}
        assert pst_fish.direction_stale_reasons(row, row.text, {}, {}, manual) == []
