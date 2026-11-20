"""Unit tests for Drama 3 pipeline status and next-step text."""

import json
import types
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_platform = sys.platform
sys.platform = "linux"
try:
    import pst_pipeline
    from pst_revoice import PlanRow
finally:
    sys.platform = _platform


def _row(strref: int, speaker: str, text: str, voice: str = "") -> PlanRow:
    return PlanRow(strref, speaker, "generate", f"VV{strref}", text, voice)


class TestOriginalCast:
    def test_voice_clip_names(self, tmp_path):
        (tmp_path / "Annah 1.WAV").write_bytes(b"x")
        (tmp_path / "Annah 2.WAV").write_bytes(b"x")
        (tmp_path / "Morte 1.txt").write_text("hi")
        (tmp_path / "notes.md").write_text("x")
        assert pst_pipeline.voice_clip_names(tmp_path) == {"Annah", "Morte"}

    def test_status_ignores_unvoiced_npcs(self, tmp_path):
        (tmp_path / "Pharod 1.WAV").write_bytes(b"x")
        have, needed, missing = pst_pipeline.original_cast_status(tmp_path)
        assert have == 0
        assert needed == len(pst_pipeline.ORIGINAL_VOICED)
        assert "Morte" in missing
        assert "Pharod" not in missing

    def test_complete_when_original_cast_present(self, tmp_path):
        for name in pst_pipeline.ORIGINAL_VOICED:
            (tmp_path / f"{name} 1.WAV").write_bytes(b"x")
        have, needed, missing = pst_pipeline.original_cast_status(tmp_path)
        assert have == needed
        assert missing == []

    def test_extract_not_next_when_cast_complete(self):
        key, message = pst_pipeline.next_action(pst_pipeline.PipelineStatus(
            lines=0, directed=0, need_direct=0, generated=0, need_audio=0,
            wow_missing=0, strata="loaded", drama3="ready",
            cloned_voices=True, cloned_count=12, cloned_needed=12,
            mood_refs=False, mood_labels=False))
        assert key != "extract"
        assert "Extract cloned" not in message

    def test_extract_next_when_cast_incomplete(self):
        key, message = pst_pipeline.next_action(pst_pipeline.PipelineStatus(
            lines=0, directed=0, need_direct=0, generated=0, need_audio=0,
            wow_missing=0, strata="loaded", drama3="ready",
            cloned_voices=False, cloned_count=10, cloned_needed=12))
        assert key == "extract"
        assert "10 / 12" in message


class TestHasSpeech:
    def test_quoted_speech(self):
        row = _row(1, "Morte", '"Hey, chief."')
        assert pst_pipeline.has_speech(row, row.text)

    def test_narration_only(self):
        row = _row(2, "Morte", "He pauses.")
        assert not pst_pipeline.has_speech(row, row.text)


class TestWowMissing:
    def test_only_used_wow_voices_without_text(self):
        plan = [
            _row(1, "Morte", '"Hi."'),
            _row(2, "Pharod", '"Oi."', "wow:broken"),
            _row(3, "Creeden", '"Hoy."', "wow:ok"),
            _row(4, "Ingress", '"Hello."', "nwn:unused"),
        ]
        library = {
            "wow:broken": {"id": "wow:broken", "reference_text": ""},
            "wow:ok": {"id": "wow:ok", "reference_text": "Hello there."},
            "wow:other": {"id": "wow:other", "reference_text": ""},
        }
        assert pst_pipeline.wow_voices_without_transcript(plan, library) == ["wow:broken"]


class TestCollectStatus:
    def test_counts(self, tmp_path):
        plan = [
            _row(1, "Morte", '"Hey, chief."'),
            _row(2, "Morte", "He pauses."),
            _row(3, "Annah", '"Piss off."'),
        ]
        (tmp_path / "VV1.WAV").write_bytes(b"x")
        ready = dict(dialog_report=True, cloned_voices=True, mood_refs=True, mood_labels=True,
                     voice_library=True, casting=True)
        status = pst_pipeline.collect_status(
            plan, {1: {"source": "strata"}}, {}, tmp_path, library={},
            strata="loaded", drama3="ready", setup=ready)
        assert status.lines == 3
        assert status.directed == 1
        assert status.need_direct == 1   # Annah; Morte 2 is narration-only
        assert status.generated == 1
        assert status.need_audio == 2
        assert status.wow_missing == 0
        assert status.examine == 0

    def test_examine_counts(self, tmp_path):
        plan = [
            _row(1, "Morte", '"Hey, chief."'),
            _row(828, "Narrator", "A bloody cloth covers the remains of this corpse."),
        ]
        (tmp_path / "VV828.WAV").write_bytes(b"x")
        ready = dict(dialog_report=True, cloned_voices=True, mood_refs=True, mood_labels=True,
                     voice_library=True, casting=True)
        status = pst_pipeline.collect_status(
            plan, {}, {}, tmp_path, library={}, strata="loaded", drama3="ready", setup=ready)
        assert status.examine == 1
        assert status.examine_audio == 1
        assert status.generated == 1
        assert status.need_direct == 1

    def test_original_lines_skip_direct(self, tmp_path):
        from pst_revoice import PlanRow
        plan = [
            PlanRow(1, "Morte", "generate", "VV1", '"Hey, chief."', "", "MRT001"),
            PlanRow(2, "Morte", "generate", "VV2", 'He grins. "Hey, chief."', "", "MRT002"),
            _row(3, "Annah", '"Piss off."'),
        ]
        ready = dict(dialog_report=True, cloned_voices=True, mood_refs=True, mood_labels=True,
                     voice_library=True, casting=True)
        status = pst_pipeline.collect_status(
            plan, {}, {}, tmp_path, library={}, strata="loaded", drama3="ready", setup=ready)
        assert status.need_direct == 1
        assert status.original_lines == 2
        assert status.original_mixed == 1
        assert status.directed == 0


class TestNextStep:
    def _status(self, **kwargs):
        base = dict(lines=10, directed=2, need_direct=0, generated=2, need_audio=8,
                    wow_missing=0, strata="loaded", drama3="ready")
        base.update(kwargs)
        return pst_pipeline.PipelineStatus(**base)

    def test_empty_plan(self):
        assert "plan" in pst_pipeline.next_step(self._status(lines=0)).lower()

    def test_dialog_report_first(self):
        text = pst_pipeline.next_step(self._status(dialog_report=False, lines=0))
        assert "Dialog report" in text

    def test_cast_needs_strata(self):
        text = pst_pipeline.next_step(self._status(casting=False, strata="not running", lines=0))
        assert "Cast NPCs" in text
        assert "PolyStrata" in text

    def test_mood_refs_need_labels(self):
        text = pst_pipeline.next_step(self._status(mood_refs=False, mood_labels=False, lines=0))
        assert "Collect clone clips" in text

    def test_direct_when_strata_down(self):
        text = pst_pipeline.next_step(self._status(need_direct=5, strata="not running"))
        assert "Direct missing" in text
        assert "PolyStrata" in text

    def test_direct_when_strata_up(self):
        text = pst_pipeline.next_step(self._status(need_direct=5, strata="loaded"))
        assert "Direct missing" in text
        assert "overnight" in text

    def test_wow_after_directing(self):
        text = pst_pipeline.next_step(self._status(need_direct=0, wow_missing=4))
        assert "Transcribe WoW" in text

    def test_generate(self):
        text = pst_pipeline.next_step(self._status(need_direct=0, need_audio=100))
        assert "Generate missing" in text
        assert "Run overnight" in text

    def test_listen_then_build(self):
        text = pst_pipeline.next_step(self._status(need_direct=0, need_audio=0, generated=10))
        assert "Build mod" in text

    def test_setup_line_marks_missing(self):
        text = pst_pipeline.setup_line(self._status(dialog_report=False, lines=0, drama3="ready"))
        assert "✗ Dialog report: missing" in text
        assert "✗ Plan: none" in text
        assert "✓ Drama 3: ready" in text

    def test_work_items_show_remaining(self):
        texts = [text for _ok, text in pst_pipeline.work_items(self._status(
            lines=100, directed=10, need_direct=90, generated=4, need_audio=96, wow_missing=3))]
        assert "Directed 10 / 100 (90 left)" in texts
        assert "Audio 4 / 100 (96 left)" in texts
        assert any("Examine 0 /" in t and "not imported" in t for t in texts)
        html = pst_pipeline.work_html(self._status(
            lines=100, directed=10, need_direct=90, generated=4, need_audio=96))
        assert "#b91c1c" in html

    def test_next_action_highlights_direct(self):
        key, message = pst_pipeline.next_action(self._status(need_direct=12, lines=20))
        assert key == "direct"
        assert "12" in message

    def test_examine_only_audio(self):
        text = pst_pipeline.next_step(self._status(
            need_direct=0, need_audio=10, examine=10, examine_audio=0, generated=0))
        assert "Generate examine" in text

    def test_floats_only_audio(self):
        key, message = pst_pipeline.next_action(self._status(
            need_direct=0, need_audio=5, floats=5, floats_audio=0, generated=0))
        assert key == "generate_floats"
        assert "idle barks" in message.lower()

    def test_work_items_idle_barks(self):
        texts = [text for _ok, text in pst_pipeline.work_items(self._status(
            lines=20, floats=4, floats_audio=1, floats_stale=1, floats_total=782))]
        joined = " ".join(texts)
        assert "Idle barks 4 / 782 in plan" in joined
        assert "audio 1 / 4" in joined
        assert "778 not imported" in joined

    def test_work_items_examine_missing_and_stale(self):
        texts = [text for _ok, text in pst_pipeline.work_items(self._status(
            lines=20, examine=10, examine_audio=4, examine_stale=2, examine_total=318))]
        joined = " ".join(texts)
        assert "Examine 10 / 318 in plan" in joined
        assert "audio 4 / 10" in joined
        assert "308 not imported" in joined
        assert "6 no WAV" in joined
        assert "2 stale" in joined


class TestAudioIsStale:
    def test_missing_record_is_stale(self):
        assert pst_pipeline.audio_is_stale(None, ["a"])

    def test_matching_texts(self):
        rec = {"segments": [{"text": "a"}, {"text": "b"}]}
        assert not pst_pipeline.audio_is_stale(rec, ["a", "b"])

    def test_changed_direction(self):
        rec = {"segments": [{"text": "[old] hi"}]}
        assert pst_pipeline.audio_is_stale(rec, ["[new] hi"])


class TestStaleCounts:
    def test_work_items_include_stale(self):
        texts = [text for _ok, text in pst_pipeline.work_items(self._status(
            lines=10, directed=10, need_direct=0, stale_direct=3,
            generated=10, need_audio=0, stale_audio=2))]
        assert "Directed 10 / 10 (3 stale)" in texts
        assert "Audio 10 / 10 (2 stale)" in texts

    def test_work_items_original_vo(self):
        texts = [text for _ok, text in pst_pipeline.work_items(self._status(
            lines=10, directed=8, need_direct=0, generated=2, need_audio=8,
            original_lines=3, original_mixed=1))]
        assert any("Original VO 3 lines kept, 1 with narrator to stitch" in t for t in texts)

    def test_next_action_stale_audio(self):
        key, message = pst_pipeline.next_action(self._status(
            need_direct=0, need_audio=0, stale_audio=4, generated=10, lines=10))
        assert key == "generate"
        assert "stale" in message

    def test_plan_stale_before_direct(self):
        key, message = pst_pipeline.next_action(self._status(
            lines=10, plan_stale=True, need_direct=5))
        assert key == "plan"
        assert "newer" in message

    def test_setup_marks_stale_plan(self):
        text = pst_pipeline.setup_line(self._status(lines=10, plan_stale=True))
        assert "inputs changed" in text

    def _status(self, **kwargs):
        base = dict(lines=10, directed=2, need_direct=0, generated=2, need_audio=8,
                    wow_missing=0, strata="loaded", drama3="ready")
        base.update(kwargs)
        return pst_pipeline.PipelineStatus(**base)


class TestCollectStale:
    def test_counts_stale_when_snapshots_passed(self, tmp_path):
        plan = [_row(1, "Morte", '"Hey, chief."')]
        (tmp_path / "VV1.WAV").write_bytes(b"x")
        ready = dict(dialog_report=True, cloned_voices=True, mood_refs=True, mood_labels=True,
                     voice_library=True, casting=True)
        directions = {1: {"speaker": "Morte", "spoken": '"Old."', "voice": "Morte",
                          "segments": [{"voice": "Morte", "base": "Old.", "text": "[dry] Old."}]}}
        generation = {1: {"speaker": "Morte", "spoken": '"Old."', "voice": "Morte",
                          "segments": [{"voice": "Morte", "text": "[dry] Old."}]}}
        status = pst_pipeline.collect_status(
            plan, directions, {}, tmp_path, library={}, strata="loaded", drama3="ready",
            setup=ready, generation=generation, patcher_config={}, pronunciation={})
        assert status.stale_direct == 1
        assert status.stale_audio == 1
        assert status.need_direct == 0
        assert status.generated == 1


class TestStrataStatus:
    def test_health_model_name_without_loaded_is_server_up(self, monkeypatch):
        class Fake:
            ok = True
            text = '{"status":"ok","model":"qwen3.6-35b-a3b-ud-q4_k_m","loaded":false}'

            def json(self):
                return {"status": "ok", "model": "qwen3.6-35b-a3b-ud-q4_k_m", "loaded": False}

        monkeypatch.setattr(pst_pipeline, "requests", types.SimpleNamespace(
            get=lambda *a, **k: Fake(), RequestException=OSError))
        assert pst_pipeline.strata_status() == "server up"

    def test_health_loaded_true(self, monkeypatch):
        class Fake:
            ok = True
            text = '{"status":"ok","model":"qwen3.6-35b-a3b-ud-q4_k_m","loaded":true}'

            def json(self):
                return {"status": "ok", "model": "qwen3.6-35b-a3b-ud-q4_k_m", "loaded": True}

        monkeypatch.setattr(pst_pipeline, "requests", types.SimpleNamespace(
            get=lambda *a, **k: Fake(), RequestException=OSError))
        assert pst_pipeline.strata_status() == "loaded"


class TestOvernight:
    def _status(self, **kwargs):
        base = dict(lines=10, directed=2, need_direct=0, generated=2, need_audio=8,
                    wow_missing=0, strata="loaded", drama3="ready")
        base.update(kwargs)
        return pst_pipeline.PipelineStatus(**base)

    def test_blocker_when_setup_missing(self):
        assert "Dialog report" in (pst_pipeline.overnight_blocker(
            self._status(dialog_report=False)) or "")
        assert pst_pipeline.overnight_blocker(self._status()) is None

    def test_order_imports_then_direct_then_free_gpu_then_generate(self):
        keys = [s.key for s in pst_pipeline.overnight_steps(self._status(
            need_direct=5, stale_direct=1, need_audio=8, stale_audio=2,
            examine=0, examine_total=318, floats=0, floats_total=782))]
        assert keys.index("import_examine") < keys.index("direct")
        assert keys.index("import_floats") < keys.index("direct")
        assert keys.index("ensure_strata") < keys.index("direct")
        assert keys.index("direct") < keys.index("free_gpu")
        assert keys.index("free_gpu") < keys.index("generate")
        assert keys[-1] == "generate"
        assert keys.count("free_gpu") >= 1

    def test_resume_skips_finished_work(self):
        keys = [s.key for s in pst_pipeline.overnight_steps(self._status(
            need_direct=0, need_audio=3, examine=318, examine_total=318,
            floats=782, floats_total=782))]
        assert "direct" not in keys
        assert "import_floats" not in keys
        assert "import_examine" not in keys
        assert keys[0] == "free_gpu"
        assert "generate" in keys

    def test_empty_when_caught_up(self):
        assert pst_pipeline.overnight_steps(self._status(
            need_direct=0, need_audio=0, stale_audio=0, generated=10,
            examine=318, examine_audio=318, examine_total=318,
            floats=782, floats_audio=782, floats_total=782)) == []


class TestLoadUniqueDialogs:
    def test_prefers_npcs_json(self, tmp_path):
        npcs = tmp_path / "npcs.json"
        npcs.write_text(json.dumps([
            {"dialog": "ATHUG", "lines": [{"strref": 10}, {"strref": 11}]},
            {"dialog": "ATHUG", "lines": [{"strref": 11}, {"strref": 12}]},
            {"dialog": "D300MER1", "lines": [{"strref": 20}]},
        ]), encoding="utf-8")
        csv_path = tmp_path / "dialog.csv"
        csv_path.write_text("StrRef,SystemName\n99,OTHER\n", encoding="utf-8")
        dialogs = pst_pipeline.load_unique_dialogs(npcs_path=npcs, csv_path=csv_path)
        assert dialogs["ATHUG"] == [10, 11, 12]
        assert dialogs["D300MER1"] == [20]
        assert "OTHER" not in dialogs

    def test_csv_fallback_skips_empty_system_name(self, tmp_path):
        csv_path = tmp_path / "dialog.csv"
        csv_path.write_text(
            "StrRef,SystemName,RealName\n1,ATHUG,Thug\n2,ATHUG,Thug\n3,,Item\n",
            encoding="utf-8")
        dialogs = pst_pipeline.load_unique_dialogs(
            npcs_path=tmp_path / "missing.json", csv_path=csv_path)
        assert dialogs == {"ATHUG": [1, 2]}


class TestSpotCheck:
    def _row(self, strref, speaker, text, voice="", original=""):
        return PlanRow(strref, speaker, "generate", f"VV{strref}", text, voice, original)

    def test_picks_longest_voiceable_among_unsampled_dialogs(self, tmp_path):
        plan = [
            self._row(90101, "Thug", '"Pay up."'),
            self._row(90102, "Thug",
                      '"Little birdy lost his way, eh? Little birdy wants to pass today."'),
            self._row(90201, "Merchant", '"Buy a blade."'),
        ]
        dialogs = {"ATHUG": [90101, 90102], "D300MER1": [90201]}
        pick = pst_pipeline.pick_spot_check(plan, dialogs, audio_dir=tmp_path)
        assert pick is not None
        assert pick.dialog == "ATHUG"
        assert pick.row.strref == 90102

    def test_skips_dialog_with_one_current_wav(self, tmp_path):
        plan = [
            self._row(90101, "Thug", '"Short."'),
            self._row(90102, "Thug", '"This is the much longer line we would have picked."'),
            self._row(90201, "Merchant", '"Buy some of the finest steel in Sigil, cutter."'),
        ]
        (tmp_path / "VV90101.WAV").write_bytes(b"x")
        generation = {90101: {"speaker": "Thug", "voice": "Thug"}}
        pick = pst_pipeline.pick_spot_check(
            plan, {"ATHUG": [90101, 90102], "D300MER1": [90201]},
            audio_dir=tmp_path, generation=generation)
        assert pick is not None
        assert pick.dialog == "D300MER1"
        assert pick.row.strref == 90201

    def test_requalifies_when_speaker_changed(self, tmp_path):
        plan = [
            self._row(90101, "NewThug", '"Short."'),
            self._row(90102, "NewThug", '"This is the much longer line after recast."'),
        ]
        (tmp_path / "VV90101.WAV").write_bytes(b"x")
        generation = {90101: {"speaker": "OldThug", "voice": "OldThug"}}
        pick = pst_pipeline.pick_spot_check(
            plan, {"ATHUG": [90101, 90102]}, audio_dir=tmp_path, generation=generation)
        assert pick is not None
        assert pick.row.strref == 90102

    def test_requalifies_when_voice_changed(self, tmp_path):
        plan = [self._row(90101, "Thug", '"A decent length spoken line here."', "nwn:new")]
        (tmp_path / "VV90101.WAV").write_bytes(b"x")
        generation = {90101: {"speaker": "Thug", "voice": "nwn:old"}}
        pick = pst_pipeline.pick_spot_check(
            plan, {"ATHUG": [90101]}, audio_dir=tmp_path, generation=generation)
        assert pick is not None
        assert pick.row.strref == 90101

    def test_requalifies_when_direction_stale(self, tmp_path):
        text = '"A decent length spoken line here."'
        plan = [self._row(90101, "Thug", text)]
        (tmp_path / "VV90101.WAV").write_bytes(b"x")
        generation = {90101: {"speaker": "Thug", "voice": "Thug"}}
        directions = {90101: {"speaker": "SomeoneElse", "spoken": text, "voice": "Thug",
                              "segments": []}}
        pick = pst_pipeline.pick_spot_check(
            plan, {"ATHUG": [90101]}, audio_dir=tmp_path,
            generation=generation, directions=directions,
            patcher_config={}, pronunciation={})
        assert pick is not None
        assert pick.row.strref == 90101

    def test_wav_without_generation_still_counts_as_sampled(self, tmp_path):
        plan = [
            self._row(90101, "Thug", '"Hello there friend."'),
            self._row(90201, "Merchant", '"Buy a blade please."'),
        ]
        (tmp_path / "VV90101.WAV").write_bytes(b"x")
        pick = pst_pipeline.pick_spot_check(
            plan, {"ATHUG": [90101], "MERCH": [90201]}, audio_dir=tmp_path)
        assert pick is not None
        assert pick.dialog == "MERCH"

    def test_skips_narration_only_and_original_vo(self, tmp_path):
        plan = [
            self._row(90101, "Thug", "He stares at you."),
            PlanRow(90102, "Morte", "generate", "VV90102",
                    '"Hey chief this is long enough."', "", "MRT001"),
            self._row(90201, "Merchant", '"Hi."'),
        ]
        pick = pst_pipeline.pick_spot_check(
            plan, {"ATHUG": [90101], "DMORTE": [90102], "MERCH": [90201]},
            audio_dir=tmp_path)
        assert pick is not None
        assert pick.dialog == "MERCH"
        assert pick.row.strref == 90201

    def test_none_when_everything_sampled(self, tmp_path):
        plan = [self._row(90101, "Thug", '"Hello."')]
        (tmp_path / "VV90101.WAV").write_bytes(b"x")
        generation = {90101: {"speaker": "Thug", "voice": "Thug"}}
        assert pst_pipeline.pick_spot_check(
            plan, {"ATHUG": [90101]}, audio_dir=tmp_path, generation=generation) is None

    def test_mixed_line_is_preferred_when_longer(self, tmp_path):
        plan = [
            self._row(90101, "Thug", '"Short."'),
            self._row(90102, "Thug",
                      'The thug bows low. "I do hope we will be doing business again soon, friend."'),
        ]
        pick = pst_pipeline.pick_spot_check(plan, {"ATHUG": [90101, 90102]}, audio_dir=tmp_path)
        assert pick is not None
        assert pick.row.strref == 90102
        assert pick.speech_chars > 20
        assert pick.total_chars > pick.speech_chars

    def test_does_not_requalify_for_unrelated_audio_drift(self, tmp_path):
        plan = [self._row(90101, "Thug", '"New words that are fairly long."')]
        (tmp_path / "VV90101.WAV").write_bytes(b"x")
        generation = {90101: {"speaker": "Thug", "voice": "Thug", "spoken": '"Old."'}}
        assert pst_pipeline.pick_spot_check(
            plan, {"ATHUG": [90101]}, audio_dir=tmp_path, generation=generation) is None

    def test_skips_dtest_dummy_dialog(self, tmp_path):
        plan = [
            self._row(90101, "Testocles", '"' + ("All work and no play. " * 40) + '"'),
            self._row(90201, "Merchant", '"Buy a blade."'),
        ]
        pick = pst_pipeline.pick_spot_check(
            plan, {"DTEST": [90101], "MERCH": [90201]}, audio_dir=tmp_path)
        assert pick is not None
        assert pick.dialog == "MERCH"

    def test_remaining_count_matches_unsampled_dialogs(self, tmp_path):
        plan = [
            self._row(90101, "Thug", '"Hello there."'),
            self._row(90201, "Merchant", '"Buy a blade."'),
            self._row(90301, "Guard", '"Move along."'),
        ]
        (tmp_path / "VV90101.WAV").write_bytes(b"x")
        generation = {90101: {"speaker": "Thug", "voice": "Thug"}}
        dialogs = {"ATHUG": [90101], "MERCH": [90201], "GUARD": [90301]}
        picks = pst_pipeline.spot_check_picks(
            plan, dialogs, audio_dir=tmp_path, generation=generation)
        assert {p.dialog for p in picks} == {"MERCH", "GUARD"}
