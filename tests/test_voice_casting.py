import struct

import pst_cast
from libs import aurora


def test_read_2da_blanks_and_quotes():
    data = b'2DA V2.0\n\n   LABEL RESREF STRREF\n0  Aasimar "c aas" 8291\n1  Empty **** ****\n'
    rows = aurora.read_2da(data)
    assert rows[0] == {"_row": "0", "LABEL": "Aasimar", "RESREF": "c aas", "STRREF": "8291"}
    assert rows[1]["RESREF"] == "" and rows[1]["STRREF"] == ""


def test_read_ssf_entries():
    header = b"SSF V1.1" + struct.pack("<II", 2, 40) + b"\x00" * 24
    table = struct.pack("<II", 48, 68)
    entries = b"vs_hello".ljust(16, b"\x00") + struct.pack("<i", 123) + b"".ljust(16, b"\x00") + struct.pack("<i", -1)
    assert aurora.read_ssf(header + table + entries) == [("vs_hello", 123), ("", -1)]


def test_wav_payload_strips_fake_header():
    real = b"RIFF" + b"\x00" * 40
    assert aurora.wav_payload(b"RIFF" + b"\x00" * 466 + real) == real
    assert aurora.wav_payload(real) == real


def test_pick_voice_validates_id():
    cands = [{"id": "nwn:BeggerM1"}, {"id": "wow:undeadmalestandardnpc"}]
    assert pst_cast.pick_voice('{"reason": "old beggar", "voice_id": "nwn:BeggerM1"}', cands) == \
        ("nwn:BeggerM1", "old beggar")
    assert pst_cast.pick_voice("I'd go with wow:undeadmalestandardnpc here", cands)[0] == "wow:undeadmalestandardnpc"
    assert pst_cast.pick_voice('{"voice_id": "nwn:Nope"}', cands)[0] is None


def test_candidates_filter_by_gender():
    voices = [{"id": "a", "gender": "male"}, {"id": "b", "gender": "female"}]
    assert [v["id"] for v in pst_cast.candidates_for({"gender": "Female"}, voices)] == ["b"]
    assert [v["id"] for v in pst_cast.candidates_for({"gender": "neutral"}, voices)] == ["a"]
    assert [v["id"] for v in pst_cast.candidates_for({"gender": "?"}, voices, "female")] == ["b"]


def test_dialog_text_truncates():
    npc = {"line_count": 3, "lines": [{"text": "a" * 10}, {"text": "b" * 10}, {"text": "c" * 10}]}
    out = pst_cast.dialog_text(npc, limit=15)
    assert out.startswith("a" * 10) and "2 more lines cut" in out


def test_pick_voice_ignores_wrong_prefix_and_case():
    cands = [{"id": "nwn:Sobrey"}, {"id": "nwn:fthugf_pcv"}]
    assert pst_cast.pick_voice('{"voice_id": "wow:Sobrey"}', cands)[0] == "nwn:Sobrey"
    assert pst_cast.pick_voice('{"voice_id": "FTHUGF_PCV"}', cands)[0] == "nwn:fthugf_pcv"


def test_catalog_never_offers_pst_voices(monkeypatch, tmp_path):
    cat = tmp_path / "c.json"
    cat.write_text('[{"id": "nwn:A", "source": "Neverwinter Nights"},'
                   ' {"id": "pst:Annah", "source": "Planescape: Torment"}]', encoding="utf-8")
    monkeypatch.setattr(pst_cast, "CATALOGS", [cat])
    assert [v["id"] for v in pst_cast.load_catalog()] == ["nwn:A"]


VOICES = [{"id": "nwn:DRedM", "gender": "male", "race": "Dragon"},
          {"id": "nwn:DGreenF", "gender": "female", "race": "Dragon"},
          {"id": "nwn:DrawlM", "gender": "male", "race": "Wraith"},
          {"id": "nwn:ThugM1", "gender": "male", "race": "Human"}]
RULES = {"exclude_voices": ["nwn:ThugM1"], "only_for": {"nwn:DrawlM": ["ghost"]},
         "force_races": [{"when": ["abishai"], "races": ["Dragon"]}]}


def test_rules_force_dragon_for_abishai():
    npc = {"speaker": "Black Abishai", "creatures": []}
    males = [v for v in VOICES if v["gender"] == "male"]
    assert [v["id"] for v in pst_cast.apply_rules(males, npc, {}, VOICES, RULES)] == ["nwn:DRedM"]


def test_rules_only_for_and_exclude():
    males = [v for v in VOICES if v["gender"] == "male"]
    human = {"speaker": "Pharod", "creatures": []}
    ghost = {"speaker": "Restless Spirit", "creatures": []}
    assert [v["id"] for v in pst_cast.apply_rules(males, human, {"type": "human"}, VOICES, RULES)] == ["nwn:DRedM"]
    assert "nwn:DrawlM" in [v["id"] for v in pst_cast.apply_rules(males, ghost, {"type": "ghost"}, VOICES, RULES)]
