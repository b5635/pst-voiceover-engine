"""Unit tests for ARE info-point examine-line extraction and plan merge."""

import importlib.util
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_platform = sys.platform
sys.platform = "linux"
try:
    _spec = importlib.util.spec_from_file_location(
        "pst_infopoints", Path(__file__).parent.parent / "pst_infopoints.py")
    assert _spec is not None and _spec.loader is not None
    inf = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(inf)
finally:
    sys.platform = _platform

PlanRow = inf.PlanRow
NARRATOR = inf.NARRATOR


def pack_region(*, typ=1, strref=100, dlg="") -> bytes:
    rec = bytearray(0xC4)
    struct.pack_into("<H", rec, 0x20, typ)
    struct.pack_into("<I", rec, 0x64, strref)
    if dlg:
        raw = dlg.encode("ascii")[:8]
        rec[0xBC:0xBC + len(raw)] = raw
    return bytes(rec)


def _row(strref: int, speaker: str = "Morte", text: str = '"Hi."') -> PlanRow:
    return PlanRow(strref, speaker, "generate", f"VV{strref}", text, "")


class TestRowsFromRegions:
    def test_unique_info_text_only(self):
        regions = [
            {"type": 1, "strref": 10, "dlg": ""},
            {"type": 1, "strref": 10, "dlg": ""},
            {"type": 1, "strref": 11, "dlg": "DSSKULL"},
            {"type": 0, "strref": 12, "dlg": ""},
            {"type": 1, "strref": 0, "dlg": ""},
            {"type": 1, "strref": 0xFFFFFFFF, "dlg": ""},
            {"type": 1, "strref": 13, "dlg": ""},
        ]
        texts = {10: "Vines with black leaves.", 11: "too far", 12: "trap", 13: "A stove."}
        rows = inf.rows_from_regions(regions, texts)
        assert [row["StrRef"] for row in rows] == [10, 13]
        assert rows[0]["SoundResRef"] == "VV10"
        assert rows[0]["Speaker"] == NARRATOR
        assert rows[0]["Action"] == "generate"
        assert rows[1]["Text"] == "A stove."

    def test_skip_plan_and_empty_text(self):
        regions = [
            {"type": 1, "strref": 10, "dlg": ""},
            {"type": 1, "strref": 14, "dlg": ""},
        ]
        texts = {10: "keep", 14: ""}
        rows = inf.rows_from_regions(regions, texts, skip={10})
        assert rows == []

    def test_parse_are_region(self):
        body = pack_region(typ=1, strref=16620)
        header = bytearray(0x100)
        header[0:8] = b"AREAV1.0"
        struct.pack_into("<H", header, 0x5A, 1)
        struct.pack_into("<I", header, 0x5C, 0x100)
        regions = inf.parse_regions(bytes(header) + body)
        assert len(regions) == 1
        assert regions[0]["type"] == 1
        assert regions[0]["strref"] == 16620
        assert regions[0]["dlg"] == ""

    def test_fallback_fits_resref(self):
        assert len(inf.fallback_resref(68032)) <= 8
        assert inf.fallback_resref(16620) == "VV16620"


class TestMerge:
    def test_no_duplicate_strrefs_and_sort(self):
        plan = [_row(5), _row(900, NARRATOR, "already")]
        extra = inf.dicts_to_plan([
            {"StrRef": 828, "Speaker": NARRATOR, "Action": "generate",
             "SoundResRef": "VV828", "Text": "A cloth.", "Voice": ""},
            {"StrRef": 5, "Speaker": NARRATOR, "Action": "generate",
             "SoundResRef": "VV5", "Text": "skip me", "Voice": ""},
        ])
        merged, added = inf.merge_into_plan(plan, extra)
        assert added == 1
        assert [p.strref for p in merged] == [5, 828, 900]
        assert merged[1].speaker == NARRATOR
        assert merged[0].speaker == "Morte"

    def test_keep_existing_on_plan_rebuild(self):
        rebuilt = [_row(1), _row(5)]
        previous = [_row(1), _row(828, NARRATOR, "A cloth.")]
        kept = inf.keep_existing(rebuilt, previous)
        assert [p.strref for p in kept] == [1, 5, 828]
        assert kept[2].is_infopoint


class TestChecksum:
    def test_verify_rejects_wrong_count(self):
        rows = [{"StrRef": 1, "Text": "x"}]
        try:
            inf.verify(rows)
        except SystemExit as e:
            assert "differs" in str(e)
        else:
            raise AssertionError("expected SystemExit")
