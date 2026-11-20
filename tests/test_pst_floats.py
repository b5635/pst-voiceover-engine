"""Unit tests for BCS FloatMessage / FloatMessageRnd idle-bark extraction."""

import importlib.util
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_platform = sys.platform
sys.platform = "linux"
try:
    _spec = importlib.util.spec_from_file_location(
        "pst_floats", Path(__file__).parent.parent / "pst_floats.py")
    assert _spec is not None and _spec.loader is not None
    flt = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(flt)
finally:
    sys.platform = _platform

PlanRow = flt.PlanRow
NARRATOR = flt.NARRATOR

SAMPLE_FLOAT = (
    b"AC\n210OB\n0 0 0 0 0 0 0 0 0 0 0 0 0 0 [-1.-1.-1.-1] \"\"OB\n"
    b"OB\n0 0 0 0 0 0 0 0 0 1 0 0 0 0 [-1.-1.-1.-1] \"\"OB\n"
    b"OB\n0 0 0 0 0 0 0 0 0 0 0 0 0 0 [-1.-1.-1.-1] \"\"OB\n"
    b"32196 0 0 0 0\"\" \"\" AC\n"
)
SAMPLE_RND = (
    b"AC\n212OB\n0 0 0 0 0 0 0 0 0 0 0 0 0 0 [-1.-1.-1.-1] \"\"OB\n"
    b"OB\n0 0 0 0 0 0 0 0 0 1 0 0 0 0 [-1.-1.-1.-1] \"\"OB\n"
    b"OB\n0 0 0 0 0 0 0 0 0 0 0 0 0 0 [-1.-1.-1.-1] \"\"OB\n"
    b"0 0 0 0 0\"1500aca2\" \"\" AC\n"
)


def _row(strref: int, speaker: str = "Morte", text: str = '"Hi."') -> PlanRow:
    return PlanRow(strref, speaker, "generate", f"VV{strref}", text, "")


class TestParse:
    def test_parse_floatmessage(self):
        refs, pools = flt.parse_bcs_floats(SAMPLE_FLOAT)
        assert refs == [32196]
        assert pools == []

    def test_parse_floatmessagernd(self):
        refs, pools = flt.parse_bcs_floats(SAMPLE_RND)
        assert refs == []
        assert pools == ["1500ACA2"]

    def test_src_entries(self):
        blob = struct.pack("<I", 2) + struct.pack("<II", 51046, 1) + struct.pack("<II", 51047, 1)
        assert flt.parse_src(blob) == [51046, 51047]

    def test_csv_stale_ini_unknown(self):
        rows = [{"StrRef": 25828, "Speaker": "Unknown", "Text": '"Ssss..."'}]
        assert flt._csv_stale(rows)
        rows[0]["Speaker"] = "Ghoul"
        assert not flt._csv_stale(rows)

    def test_looks_spoken(self):
        assert flt.looks_spoken('"Deaders?"')
        assert flt.looks_spoken("::Yawn::")
        assert flt.looks_spoken("*Oof*")
        assert flt.looks_spoken('"Them boxes sure are heavy!"')
        assert not flt.looks_spoken("POUCH Weight: 0  A small bag.")
        assert not flt.looks_spoken("Morte Taunts 1")
        assert not flt.looks_spoken("Ghoul Male Battle Cry 1 (Approach 1)")
        assert not flt.looks_spoken("")

    def test_fallback_fits_resref(self):
        assert len(flt.fallback_resref(68077)) <= 8
        assert flt.fallback_resref(8161) == "VV8161"


class TestIdentity:
    def test_skip_plan_and_labels(self):
        hits = {8161: ["0100MHUL"], 61943: ["X"], 10: ["0100MHUL"]}
        script_dialogs = {"0100MHUL": ["DMHULT"]}
        script_names = {"0100MHUL": ["Mhult"]}
        texts = {
            8161: '"Deaders?"',
            61943: "Morte Taunts 1",
            10: '"already in plan"',
        }
        speakers = {"DMHULT": "Mhult"}
        voices = {"DMHULT": "nwn:demo"}
        rows = flt.rows_from_hits(hits, script_dialogs, script_names, texts,
                                  speakers, voices, skip={10})
        assert [row["StrRef"] for row in rows] == [8161]
        assert rows[0]["Speaker"] == "Mhult"
        assert rows[0]["Voice"] == "nwn:demo"
        assert rows[0]["SoundResRef"] == "VV8161"
        assert rows[0]["Action"] == "generate"

    def test_shared_strref_picks_common_dialog(self):
        hits = {27432: ["A", "B", "B"]}
        script_dialogs = {"A": ["DLG1"], "B": ["DLG2"]}
        texts = {27432: '"Help! Guards! I\'m being attacked!"'}
        speakers = {"DLG1": "One", "DLG2": "Two"}
        voices = {"DLG2": "nwn:two"}
        rows = flt.rows_from_hits(hits, script_dialogs, {}, texts, speakers, voices)
        assert rows[0]["Speaker"] == "Two"
        assert rows[0]["Voice"] == "nwn:two"

    def test_cre_long_name_when_no_dialog(self):
        hits = {25832: ["GHOULSC"]}
        rows = flt.rows_from_hits(
            hits, {}, {"GHOULSC": ["Ghoul", "Ghoul"]}, {25832: "::Growl::"},
            {}, {}, skip=set())
        assert rows[0]["Speaker"] == "Ghoul"
        assert rows[0]["Voice"] == ""
        assert rows[0]["Speaker"] != NARRATOR

    def test_reuse_dlg_voice_for_same_text(self):
        hits = {8161: ["COLLECT"]}
        script_dialogs = {"COLLECT": ["DBVCOLL"]}
        texts = {8161: '"Deaders?"'}
        speakers = {"DBVCOLL": "Collector"}
        voices = {"DBVCOLL": "nwn:BeggerM1"}
        dlg_by_text = {'"Deaders?"': [("Collector", "nwn:BeggerM1")]}
        rows = flt.rows_from_hits(hits, script_dialogs, {}, texts, speakers, voices, dlg_by_text)
        assert rows[0]["Speaker"] == "Collector"
        assert rows[0]["Voice"] == "nwn:BeggerM1"

    def test_never_narrator(self):
        speaker, voice = flt.pick_identity(["DNAR"], ["Worker"], {"DNAR": NARRATOR}, {}, "x", {})
        assert speaker == "Worker"
        assert speaker != NARRATOR

    def test_unknown_when_no_cre_or_dialog(self):
        speaker, voice = flt.pick_identity([], [], {}, {}, '"Hey."', {})
        assert speaker == "Unknown"
        assert speaker != NARRATOR

    def test_parse_are_actor(self):
        rec = bytearray(0x110)
        rec[0x48:0x50] = b"DACASTE"
        rec[0x50:0x58] = b"1200ACS"
        rec[0x80:0x88] = b"ACASTE"
        header = bytearray(0x100)
        header[0:8] = b"AREAV1.0"
        struct.pack_into("<I", header, 0x54, 0x100)
        struct.pack_into("<H", header, 0x58, 1)
        actors = flt.parse_are_actors(bytes(header) + rec)
        assert actors == [("ACASTE", "DACASTE", ["1200ACS", "", "", "", "", ""])]

    def test_token_resref_empty(self):
        assert flt.token_resref("") == ""
        assert flt.token_resref("   ") == ""
        assert flt.token_resref("1700gul1") == "1700GUL1"

    def test_parse_ini_spawn_default_script(self):
        blob = (
            b"[ghoul1]\n"
            b"cre_file\t= ghoulm01\n"
            b"script_name\t= 1700ghoul1\n"
            b"dialog\t\t= DDrnGhGd\n"
            b"script_default\t= 1700gul1\n"
            b";script_override = ignored\n"
            b"[empty]\n"
            b"script_default = 9999dead\n"
        )
        rows = flt.parse_ini_spawns(blob)
        assert len(rows) == 1
        cre, dlg, scripts = rows[0]
        assert cre == "GHOULM01"
        assert dlg == "DDRNGHGD"
        assert "1700GUL1" in scripts
        assert "1700GHOU" not in scripts  # script_name is a death variable, truncated resref
        assert "9999DEAD" not in scripts

    def test_ini_spawn_identity(self):
        hits = {25828: ["1700GUL1"]}
        rows = flt.rows_from_hits(
            hits, {"1700GUL1": ["DDRNGHGD"]}, {"1700GUL1": ["Ghoul"]},
            {25828: '"Ssss... boredom setss in..."'},
            {"DDRNGHGD": "Ghoul"}, {"DDRNGHGD": "wow:undeadmaledarknpc"},
        )
        assert rows[0]["Speaker"] == "Ghoul"
        assert rows[0]["Voice"] == "wow:undeadmaledarknpc"
        assert rows[0]["Speaker"] != "Unknown"


class TestMerge:
    def test_keep_existing_on_plan_rebuild(self, tmp_path, monkeypatch):
        csv_path = tmp_path / "floats.csv"
        flt.write_rows([
            {"StrRef": 8161, "Speaker": "Collector", "Action": "generate",
             "SoundResRef": "VV8161", "Text": '"Deaders?"', "Voice": "nwn:BeggerM1"},
        ], csv_path)
        flt.float_strrefs.cache_clear()
        monkeypatch.setattr(flt, "FLOATS_PATH", csv_path)
        rebuilt = [_row(1), _row(5)]
        previous = [_row(1), _row(8161, "Collector", '"Deaders?"')]
        kept = flt.keep_existing(rebuilt, previous)
        assert [p.strref for p in kept] == [1, 5, 8161]
        assert kept[2].speaker == "Collector"

    def test_verify_rejects_wrong_count(self):
        try:
            flt.verify([{"StrRef": 1, "Text": "x"}])
        except SystemExit as e:
            assert "differs" in str(e)
        else:
            raise AssertionError("expected SystemExit")


class TestExistingDump:
    def test_checksum_matches_gemrb_skip_plan(self):
        path = Path("pst_revoice/floats.csv")
        if not path.exists():
            return
        rows = flt.read_rows(path)
        got = flt.checksum(rows)
        assert got == (flt.EXPECTED_COUNT, flt.EXPECTED_CHARS, flt.EXPECTED_SUM,
                       flt.EXPECTED_MIN, flt.EXPECTED_MAX)

    def test_known_rows(self):
        path = Path("pst_revoice/floats.csv")
        if not path.exists():
            return
        by_ref = {int(r["StrRef"]): r for r in flt.read_rows(path)}
        deaders = by_ref[8161]
        assert deaders["Speaker"] == "Collector"
        assert deaders["Voice"] == "nwn:BeggerM1"
        assert "Deaders?" in deaders["Text"]
        for ref in (8163, 8164, 8165):
            assert by_ref[ref]["Speaker"] == "Collector"
            assert by_ref[ref]["Voice"] == "nwn:BeggerM1"
        meat = by_ref[51046]
        assert meat["Speaker"] == "Acaste"
        assert "Look at the meat" in meat["Text"]
        ghouls = [r for r in by_ref.values() if r["Speaker"] == "Ghoul"]
        assert len(ghouls) >= 10
        for ref in (25828, 25829, 25830, 25831):
            row = by_ref[ref]
            assert row["Speaker"] == "Ghoul"
            assert row["Voice"]
            assert row["Speaker"] != "Unknown"
        assert "boredom setss" in by_ref[25828]["Text"]
        assert "new lairs" in by_ref[25829]["Text"].lower()
        assert "eyesss" in by_ref[25830]["Text"]
        assert not any(r["Speaker"] == NARRATOR for r in by_ref.values())
