"""
Unit tests for pst_revoice.py: narration/speech splitting and revoice planning.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# pst_revoice.py re-wraps stdout/stderr on win32 at import, which breaks pytest's capture
_platform = sys.platform
sys.platform = "linux"
try:
    _spec = importlib.util.spec_from_file_location("pst_revoice", Path(__file__).parent.parent / "pst_revoice.py")
    assert _spec is not None and _spec.loader is not None
    pst_revoice = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(pst_revoice)
finally:
    sys.platform = _platform

NARRATOR = pst_revoice.NARRATOR
split_segments = pst_revoice.split_segments


# ==============================================================================
# split_segments
# ==============================================================================


def test_split_narration_then_speech():
    assert split_segments('He pauses. "Then I\'ll tell you."', "Morte") == [
        (NARRATOR, "He pauses."),
        ("Morte", "Then I'll tell you."),
    ]


def test_split_speech_narration_speech_keeps_emphasis_for_rules():
    text = '"You don\'t *remember?*" He pauses for a moment. "Say, I *have* to ask."'
    assert split_segments(text, "Morte") == [
        ("Morte", "You don't *remember?*"),
        (NARRATOR, "He pauses for a moment."),
        ("Morte", "Say, I *have* to ask."),
    ]


def test_split_unclosed_quote_runs_to_end():
    assert split_segments('She frowns. "Well, if yeh must', "Annah") == [
        (NARRATOR, "She frowns."),
        ("Annah", "Well, if yeh must"),
    ]


def test_split_punctuation_between_quotes_is_dropped():
    assert split_segments('*"Yesssss..."* *"Long have I ssslept..."*', "Ignus") == [
        ("Ignus", "Yesssss... Long have I ssslept..."),
    ]


# ==============================================================================
# prepare_segment_text
# ==============================================================================


PATCHER_CONFIG = {
    "phoneticRules": [
        {"pattern": "\\*(?:sigh|groan)[!\\s]*\\*", "replacement": ""},
        {"pattern": "\\*([^*]+)\\*", "replacement": "\"$1\""},
        {"pattern": "\\bMorte\\b", "replacement": "Mort"},
    ],
}


def test_prepare_segment_text_applies_rules_then_strips_asterisks():
    assert pst_revoice.prepare_segment_text("*sigh* Fine, I *have* to.", PATCHER_CONFIG) == 'Fine, I "have" to.'


def test_prepare_segment_text_pronunciation():
    assert pst_revoice.prepare_segment_text("Morte nods. Morte's grin.", PATCHER_CONFIG) == "Mort nods. Mort's grin."


def test_prepare_segment_text_effect_only_is_empty():
    assert pst_revoice.prepare_segment_text("*groan*", PATCHER_CONFIG) == ""


def test_split_unquoted_line_is_narration():
    assert split_segments("Morte continues howling.", "Morte") == [(NARRATOR, "Morte continues howling.")]


def test_split_all_caps_line_is_speech():
    assert split_segments("SO YOU HAVE COME.", "The Transcendent One") == [
        ("The Transcendent One", "SO YOU HAVE COME."),
    ]


def test_split_asterisk_wrapped_line_is_speech():
    assert split_segments("*JUSTICE comes even to the FIENDS.*", "Vhailor") == [
        ("Vhailor", "*JUSTICE comes even to the FIENDS.*"),
    ]


def test_split_emote_is_speech():
    assert split_segments("::Yawn::", "Ghoul") == [("Ghoul", "::Yawn::")]
    assert split_segments("::Growl::", "Ghoul") == [("Ghoul", "::Growl::")]


def test_split_placeholders_are_empty():
    for text in ("NULL NODE", "Null_Node", "[no text]", "   "):
        assert split_segments(text, "Morte") == []


# ==============================================================================
# build_plan
# ==============================================================================


def row(strref: int, name: str, sound: str = "", text: str = '"Hi."'):
    return {"StrRef": str(strref), "RealName": name, "SoundResRef": sound, "Text": text}


@pytest.fixture
def own_voices(monkeypatch):
    """Morte and Annah have cloned profiles, Good Incarnation borrows the Nameless One."""
    monkeypatch.setattr(pst_revoice, "profile_voices", lambda: {"Morte", "Annah", "Narrator"})
    monkeypatch.setattr(pst_revoice, "load_voice_aliases", lambda: {"Good Incarnation": "Nameless One"})


def test_build_plan_keeps_original_resref(own_voices):
    rows = [row(1, "Morte", "MRT001"), row(2, "Morte"), row(3, "Pharod")]
    plan = pst_revoice.build_plan(rows)
    assert [(p.strref, p.speaker, p.action, p.resref, p.original) for p in plan] == [
        (1, "Morte", "generate", "VV1", "MRT001"),
        (2, "Morte", "generate", "VV2", ""),
    ]                                          # 3: Pharod has no voice yet


def test_build_plan_skips_placeholders(own_voices):
    rows = [row(1, "Morte", "MRT001"), row(2, "Morte", text="NULL NODE"), row(3, "Morte", "TS00003")]
    plan = pst_revoice.build_plan(rows)
    assert [p.strref for p in plan] == [1, 3]


def test_build_plan_speaker_filter(own_voices):
    rows = [row(1, "Morte", "MRT001"), row(2, "Morte"), row(3, "Annah", "ANA001"), row(4, "Annah")]
    plan = pst_revoice.build_plan(rows, {"Annah"})
    assert [p.strref for p in plan] == [3, 4]


def test_build_plan_includes_aliased_speakers(own_voices):
    rows = [row(1, "Good Incarnation"), row(2, "Pharod")]
    plan = pst_revoice.build_plan(rows)
    assert [(p.strref, p.speaker, p.action) for p in plan] == [(1, "Good Incarnation", "generate")]


def test_tts_segments_uses_aliased_voice(monkeypatch):
    monkeypatch.setattr(pst_revoice, "load_voice_aliases", lambda: {"Good Incarnation": "Nameless One"})
    text = 'He smiles. "I am sorry."'
    assert pst_revoice.tts_segments(text, "Good Incarnation", PATCHER_CONFIG, {}) == [
        (NARRATOR, "He smiles."),
        ("Nameless One", "I am sorry."),
    ]


def test_tra_string_handles_tildes():
    assert pst_revoice._tra_string("plain") == "~plain~"
    assert pst_revoice._tra_string("a ~ b") == "~~~~~a ~ b~~~~~"


# ==============================================================================
# Pronunciation rules and line overrides
# ==============================================================================

ANNAH_RULES = {"Annah": [
    {"pattern": r"\bee+ji?t(s?)\b", "replacement": "idiot$1"},
    {"pattern": r"\bgit\b", "replacement": "get"},
    {"pattern": r"\bh+s+s*t+\b", "replacement": "hisss"},
]}


def test_apply_pronunciation_keeps_capitals_and_groups():
    rules = ANNAH_RULES["Annah"]
    assert pst_revoice.apply_pronunciation("Git, yeh eejts!", rules) == "Get, yeh idiots!"
    assert pst_revoice.apply_pronunciation("an eejit", rules) == "an idiot"


def test_tts_segments_rules_apply_to_speaker_only():
    text = 'The eejit narrator watches. "Hsst! Ye eejit!"'
    assert pst_revoice.tts_segments(text, "Annah", PATCHER_CONFIG, ANNAH_RULES) == [
        (NARRATOR, "The eejit narrator watches."),
        ("Annah", "Hisss! Ye idiot!"),
    ]


def test_tts_segments_no_rules_for_other_speakers():
    assert pst_revoice.tts_segments('"Git."', "Morte", PATCHER_CONFIG, ANNAH_RULES) == [("Morte", "Git.")]


def test_tts_segments_merges_same_voice_after_dropped_segment():
    text = 'She frowns. "Hsst!" She turns away.'
    segs = pst_revoice.tts_segments(text, "Annah", PATCHER_CONFIG, {})
    assert segs[0][0] == NARRATOR and "She frowns" in segs[0][1]


def test_tts_segments_star_emote_becomes_sound():
    text = 'She frowns. "*sigh*" She turns away.'
    assert pst_revoice.tts_segments(text, "Annah", PATCHER_CONFIG, {}) == [
        (NARRATOR, "She frowns."), ("Annah", "Haaah..."), (NARRATOR, "She turns away."),
    ]


def test_line_overrides_round_trip(tmp_path):
    path = tmp_path / "overrides.json"
    pst_revoice.save_line_overrides({1855: '"Get ye gone."'}, path)
    assert pst_revoice.load_line_overrides(path) == {1855: '"Get ye gone."'}
    assert pst_revoice.load_line_overrides(tmp_path / "missing.json") == {}


def test_build_plan_adds_cast_npcs(own_voices):
    rows = [row(1, "Morte", "MRT001"), row(2, "Morte"), row(3, "Pharod"), row(4, "Pharod", "PHA001"), row(5, "")]
    cast = {2: ("nwn:X", "Morte"), 3: ("nwn:BeggerM1", "Pharod"), 4: ("nwn:BeggerM1", "Pharod"),
            5: ("wow:orcmalestandardnpc", "Thug")}
    plan = pst_revoice.build_plan(rows, cast=cast)
    assert [(p.strref, p.speaker, p.voice) for p in plan] == [
        (1, "Morte", ""),
        (2, "Morte", ""),                          # own cloned voice wins over a cast
        (3, "Pharod", "nwn:BeggerM1"),
        (4, "Pharod", "nwn:BeggerM1"),             # original resref kept on the plan row
        (5, "Thug", "wow:orcmalestandardnpc"),     # no RealName: the cast speaker name
    ]
    assert plan[3].original == "PHA001"
    assert plan[0].original == "MRT001"


def test_tts_segments_uses_cast_voice():
    text = 'He coughs. "Spare a copper?"'
    assert pst_revoice.tts_segments(text, "Pharod", PATCHER_CONFIG, {}, "nwn:BeggerM1") == [
        (NARRATOR, "He coughs."),
        ("nwn:BeggerM1", "Spare a copper?"),
    ]


# ==============================================================================
# Notes, generation snapshots, stale detection
# ==============================================================================


def _annah(text: str = '"Git."', voice: str = "") -> "pst_revoice.PlanRow":
    return pst_revoice.PlanRow(1, "Annah", "generate", "VV1", text, voice)


def test_line_notes_round_trip(tmp_path):
    path = tmp_path / "notes.json"
    pst_revoice.save_line_notes({1855: 'replace "ta" with "to"', 9: "   "}, path)
    assert pst_revoice.load_line_notes(path) == {1855: 'replace "ta" with "to"'}
    assert pst_revoice.load_line_notes(tmp_path / "missing.json") == {}


def test_matching_pronunciation_rules_speech_only():
    text = 'The eejit narrator watches. "Hsst! Ye eejit!"'
    matched = pst_revoice.matching_pronunciation_rules(text, "Annah", ANNAH_RULES)
    assert [r["replacement"] for r in matched] == ["idiot$1", "hisss"]
    assert pst_revoice.matching_pronunciation_rules(text, "Morte", ANNAH_RULES) == []
    assert pst_revoice.matching_pronunciation_rules('The eejit narrator watches.', "Annah", ANNAH_RULES) == []


def test_generation_snapshot_records_speaker_and_matching_rules():
    row = _annah('She nods. "Git, ye eejit."')
    snap = pst_revoice.generation_snapshot(row, row.text, PATCHER_CONFIG, ANNAH_RULES)
    assert snap["speaker"] == "Annah"
    assert snap["voice"] == "Annah"
    assert snap["spoken"] == row.text
    assert snap["tts"] == [[NARRATOR, "She nods."], ["Annah", "Get, ye idiot."]]
    assert [r["replacement"] for r in snap["rules"]] == ["idiot$1", "get"]
    assert snap["rules_fingerprint"] == pst_revoice.rules_fingerprint("Annah", ANNAH_RULES)


def test_generation_is_stale_unknown_only_when_rules_apply():
    row = _annah('"Git."')
    assert pst_revoice.generation_is_stale(row, row.text, PATCHER_CONFIG, ANNAH_RULES, None)
    clean = _annah('"Hello."')
    assert not pst_revoice.generation_is_stale(clean, clean.text, PATCHER_CONFIG, ANNAH_RULES, None)


def test_generation_is_stale_speaker_or_voice_change():
    row = _annah('"Hello."')
    snap = pst_revoice.generation_snapshot(row, row.text, PATCHER_CONFIG, {})
    other = pst_revoice.PlanRow(1, "Morte", "generate", "VV1", row.text)
    assert pst_revoice.generation_is_stale(other, row.text, PATCHER_CONFIG, {}, snap)
    cast = _annah('"Hello."', "nwn:BeggerM1")
    assert pst_revoice.generation_is_stale(cast, row.text, PATCHER_CONFIG, {}, snap)


def test_generation_is_stale_pronunciation_change_only_if_tts_changes():
    row = _annah('"Git, ye eejit."')
    snap = pst_revoice.generation_snapshot(row, row.text, PATCHER_CONFIG, ANNAH_RULES)
    assert not pst_revoice.generation_is_stale(row, row.text, PATCHER_CONFIG, ANNAH_RULES, snap)

    extra = {"Annah": ANNAH_RULES["Annah"] + [{"pattern": r"\bxyz\b", "replacement": "zzz"}]}
    assert not pst_revoice.generation_is_stale(row, row.text, PATCHER_CONFIG, extra, snap)

    changed = {"Annah": [{"pattern": r"\bgit\b", "replacement": "got"}]}
    assert pst_revoice.generation_is_stale(row, row.text, PATCHER_CONFIG, changed, snap)

    edited = '"Git, ye eejit. Also this."'
    assert pst_revoice.generation_is_stale(row, edited, PATCHER_CONFIG, ANNAH_RULES, snap)


def test_generation_round_trip(tmp_path):
    path = tmp_path / "generation.json"
    row = _annah('"Git."')
    snap = pst_revoice.generation_snapshot(row, row.text, PATCHER_CONFIG, ANNAH_RULES)
    pst_revoice.save_generation({row.strref: snap}, path)
    loaded = pst_revoice.load_generation(path)
    assert loaded[1]["speaker"] == "Annah"
    assert loaded[1]["tts"] == [["Annah", "Get."]]
    assert not pst_revoice.generation_is_stale(row, row.text, PATCHER_CONFIG, ANNAH_RULES, loaded[1])


def test_infopoint_flag():
    assert pst_revoice.PlanRow(828, NARRATOR, "generate", "VV828", "A cloth.").is_infopoint
    assert not pst_revoice.PlanRow(1, "Morte", "generate", "VV1", '"Hi."').is_infopoint


def test_build_mod_skips_string_set_for_floats(tmp_path, monkeypatch):
    import pst_floats
    fish = tmp_path / "audio_fish"
    fish.mkdir()
    (fish / "VV8161.WAV").write_bytes(b"RIFF")
    (fish / "VV1.WAV").write_bytes(b"RIFF")
    mod = tmp_path / "mod"
    monkeypatch.setattr(pst_revoice, "FISH_AUDIO_DIR", fish)
    monkeypatch.setattr(pst_revoice, "MOD_DIR", mod)
    monkeypatch.setattr(pst_floats, "is_float", lambda row, path=None: row.strref == 8161)
    plan = [
        pst_revoice.PlanRow(1, "Morte", "generate", "VV1", '"Hi."'),
        pst_revoice.PlanRow(8161, "Collector", "generate", "VV8161", '"Deaders?"', "nwn:BeggerM1"),
    ]
    pst_revoice.build_mod(plan)
    tp2 = (mod / f"setup-{pst_revoice.MOD_NAME}.tp2").read_text(encoding="utf-8")
    assert "STRING_SET 1 @" in tp2
    assert "STRING_SET 8161" not in tp2
    tra = (mod / pst_revoice.MOD_NAME / "tra" / "voices.tra").read_text(encoding="utf-8")
    assert "@8161" not in tra
    assert (mod / pst_revoice.MOD_NAME / "audio" / "VV8161.WAV").exists()


def test_build_mod_skips_string_set_for_infopoints(tmp_path, monkeypatch):
    fish = tmp_path / "audio_fish"
    fish.mkdir()
    (fish / "VV1.WAV").write_bytes(b"RIFF")
    (fish / "VV828.WAV").write_bytes(b"RIFF")
    mod = tmp_path / "mod"
    monkeypatch.setattr(pst_revoice, "FISH_AUDIO_DIR", fish)
    monkeypatch.setattr(pst_revoice, "MOD_DIR", mod)
    plan = [
        pst_revoice.PlanRow(1, "Morte", "generate", "VV1", '"Hi."'),
        pst_revoice.PlanRow(828, NARRATOR, "generate", "VV828", "A bloody cloth."),
    ]
    pst_revoice.build_mod(plan)
    tp2 = (mod / f"setup-{pst_revoice.MOD_NAME}.tp2").read_text(encoding="utf-8")
    assert "STRING_SET 1 @" in tp2
    assert "STRING_SET 828" not in tp2
    tra = (mod / pst_revoice.MOD_NAME / "tra" / "voices.tra").read_text(encoding="utf-8")
    assert "@1 =" in tra
    assert "@828" not in tra
    assert (mod / pst_revoice.MOD_NAME / "audio" / "VV828.WAV").exists()


def test_plan_stale_reasons(tmp_path, monkeypatch):
    plan = tmp_path / "plan.csv"
    assert pst_revoice.plan_stale_reasons(plan_path=plan) == ["no plan"]
    plan.write_text("StrRef,Speaker,Action,SoundResRef,Text,Voice\n", encoding="utf-8")
    monkeypatch.setattr(pst_revoice, "load_plan_meta", lambda path=None: None)
    assert "snapshot" in pst_revoice.plan_stale_reasons(plan_path=plan)[0]
    meta = {"dialog_mtime": 1, "casting_mtime": 1, "npcs_mtime": 1, "aliases_mtime": 1}
    current = {"dialog_mtime": 100, "casting_mtime": 1, "npcs_mtime": 1, "aliases_mtime": 1}
    assert pst_revoice.plan_stale_reasons(meta, current, plan) == ["dialog report is newer than the plan"]
    current_close = {"dialog_mtime": 1.2, "casting_mtime": 1, "npcs_mtime": 1, "aliases_mtime": 1}
    assert pst_revoice.plan_stale_reasons(meta, current_close, plan) == []


def test_write_plan_meta(tmp_path):
    path = tmp_path / "plan_meta.json"
    plan = [pst_revoice.PlanRow(1, "Morte", "generate", "VV1", '"Hi."')]
    pst_revoice.write_plan_meta(plan, path)
    meta = pst_revoice.load_plan_meta(path)
    assert meta["lines"] == 1
    assert meta["written_at"]


def test_needs_direction_skips_original_and_narration():
    voiced = pst_revoice.PlanRow(1, "Morte", "generate", "VV1", '"Hi."', "", "MRT001")
    silent = pst_revoice.PlanRow(2, "Morte", "generate", "VV2", '"Hi."')
    narr = pst_revoice.PlanRow(3, "Morte", "generate", "VV3", "He pauses.")
    assert not pst_revoice.needs_direction(voiced, voiced.text)
    assert pst_revoice.needs_direction(silent, silent.text)
    assert not pst_revoice.needs_direction(narr, narr.text)


def test_write_plan_roundtrip_original(tmp_path, monkeypatch):
    path = tmp_path / "plan.csv"
    plan = [pst_revoice.PlanRow(1, "Morte", "generate", "VV1", '"Hi."', "", "MRT001")]
    pst_revoice.write_plan(plan, path)
    monkeypatch.setattr(pst_revoice, "load_original_map", lambda csv_path=None: {})
    loaded = pst_revoice.read_plan(path)
    assert loaded[0].original == "MRT001"
    assert loaded[0].resref == "VV1"


def test_read_plan_fills_original_from_dialog(tmp_path, monkeypatch):
    path = tmp_path / "plan.csv"
    path.write_text(
        "StrRef,Speaker,Action,SoundResRef,Text,Voice\n"
        '1,Morte,generate,VV1,"Hi.",\n',
        encoding="utf-8")
    monkeypatch.setattr(pst_revoice, "load_original_map", lambda csv_path=None: {1: "MRT001"})
    assert pst_revoice.read_plan(path)[0].original == "MRT001"


def test_extract_original_keeps_existing(tmp_path, monkeypatch):
    dest = tmp_path / "orig"
    dest.mkdir()
    (dest / "MRT001.WAV").write_bytes(b"RIFF")
    monkeypatch.setattr(pst_revoice.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("weidu")))
    assert pst_revoice.extract_original_sounds(["MRT001"], dest) == 1
    assert (dest / "MRT001.WAV").read_bytes().startswith(b"RIFF")


def test_original_wav_finds_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pst_revoice, "ORIGINAL_DIR", tmp_path)
    monkeypatch.setattr(pst_revoice, "MOOD_CLIPS_DIR", tmp_path / "no_clips")
    (tmp_path / "MRT001.WAV").write_bytes(b"RIFF")
    found = pst_revoice.original_wav("MRT001")
    assert found is not None and found.name == "MRT001.WAV"


# ==============================================================================
# pst-cast-overrides.json (hand-picked speaker / voice)
# ==============================================================================


def test_cast_overrides_roundtrip_drops_empty(tmp_path):
    path = tmp_path / "pst-cast-overrides.json"
    pst_revoice.save_cast_overrides({
        "dialogs": {"DPHAROD": {"voice": "nwn:X", "set_at": "t"}, "DEMPTY": {"set_at": "t"}},
        "lines": {"7": {"speaker": "Morte"}},
    }, path)
    data = pst_revoice.load_cast_overrides(path)
    assert data == {"dialogs": {"DPHAROD": {"voice": "nwn:X", "set_at": "t"}},
                    "lines": {"7": {"speaker": "Morte"}}}
    assert pst_revoice.load_cast_overrides(tmp_path / "missing.json") == {"dialogs": {}, "lines": {}}


def test_cast_override_line_beats_dialog():
    ov = {"dialogs": {"DPHAROD": {"speaker": "Pharod", "voice": "nwn:A"}}, "lines": {"3": {"voice": "nwn:B"}}}
    assert pst_revoice.cast_override_for(3, ov, "dpharod") == {"speaker": "Pharod", "voice": "nwn:B"}
    assert pst_revoice.cast_override_for(4, ov, "DPHAROD") == {"speaker": "Pharod", "voice": "nwn:A"}
    assert pst_revoice.cast_override_for(4, ov, "") == {}


def test_build_plan_hand_picks_beat_casting(own_voices):
    rows = [row(1, "Morte"), row(2, "Pharod"), row(3, "Pharod"), row(4, "Dhall"), row(5, "Pharod")]
    cast = {2: ("nwn:BeggerM1", "Pharod"), 3: ("nwn:BeggerM1", "Pharod"), 5: ("nwn:BeggerM1", "Pharod")}
    overrides = {
        "dialogs": {"DPHAROD": {"voice": "wow:pharod"}},
        "lines": {"1": {"voice": "nwn:Skull"},            # own-voice character given a library voice
                  "3": {"speaker": "Annah"},              # new speaker with their own clone
                  "4": {"voice": "nwn:Scribe"}},          # uncast line becomes voiced
    }
    dialog_of = {2: "DPHAROD", 3: "DPHAROD", 5: "DPHAROD"}
    plan = pst_revoice.build_plan(rows, cast=cast, overrides=overrides, dialog_of=dialog_of)
    assert [(p.strref, p.speaker, p.voice) for p in plan] == [
        (1, "Morte", "nwn:Skull"),
        (2, "Pharod", "wow:pharod"),
        (3, "Annah", "wow:pharod"),               # dialog voice still applies on top of the line's speaker
        (4, "Dhall", "nwn:Scribe"),
        (5, "Pharod", "wow:pharod"),
    ]


def test_apply_cast_override_new_speaker_drops_library_voice(own_voices):
    r = pst_revoice.PlanRow(2, "Pharod", "generate", "VV2", '"Hi."', "nwn:BeggerM1")
    pst_revoice.apply_cast_override(r, {"speaker": "Annah"})
    assert (r.speaker, r.voice, r.speech_voice) == ("Annah", "", "Annah")
    pst_revoice.apply_cast_override(r, {"speaker": "Annah", "voice": "Annah"})
    assert r.voice == ""                          # own voice is stored as no library voice


def test_plan_stale_when_cast_overrides_newer(tmp_path):
    plan = tmp_path / "plan.csv"
    plan.write_text("StrRef,Speaker,Action,SoundResRef,Text,Voice\n", encoding="utf-8")
    meta = {"dialog_mtime": 1, "casting_mtime": 1, "npcs_mtime": 1, "aliases_mtime": 1, "cast_overrides_mtime": 1}
    current = dict(meta, cast_overrides_mtime=50)
    assert pst_revoice.plan_stale_reasons(meta, current, plan) == ["cast overrides is newer than the plan"]
