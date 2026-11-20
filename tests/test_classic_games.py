"""
Unit tests for classic (non-EE) game support, e.g. original Planescape: Torment:
- dialog.tlk / dialogF.tlk in the game root
- CRE V1.2 parsing
- WAVC detection
- quoted-speech transcripts
"""

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import libs.appconfig as appconfig  # noqa: E402
from libs.utils import find_dialog_tlk, find_dialogf_tlk  # noqa: E402


def load_script(filename: str, module_name: str):
    """Import a script whose filename contains hyphens."""
    path = Path(__file__).parent.parent / filename
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dialog_report_prepare = load_script("dialog-report-prepare.py", "dialog_report_prepare")

# profiles-prepare.py re-wraps stdout/stderr on win32 at import, which breaks pytest's capture
_platform = sys.platform
sys.platform = "linux"
try:
    profiles_prepare = load_script("profiles-prepare.py", "profiles_prepare")
finally:
    sys.platform = _platform


@pytest.fixture
def isolated_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point appconfig at a throwaway file so setting values doesn't touch the real config."""
    monkeypatch.setattr(appconfig, "_CONFIG_PATH", tmp_path / "appconfig.json")
    monkeypatch.setattr(appconfig, "_overrides", {})
    monkeypatch.setattr(appconfig, "_loaded", False)
    return appconfig.cfg


# ==============================================================================
# TLK lookup
# ==============================================================================


def test_find_dialog_tlk_prefers_lang_folder(tmp_path):
    (tmp_path / "lang" / "en_US").mkdir(parents=True)
    tlk = tmp_path / "lang" / "en_US" / "dialog.tlk"
    tlk.write_bytes(b"")
    assert find_dialog_tlk(tmp_path) == tlk


def test_find_dialog_tlk_falls_back_to_game_root(tmp_path):
    tlk = tmp_path / "Dialog.tlk"
    tlk.write_bytes(b"")
    assert find_dialog_tlk(tmp_path) == tlk


def test_find_dialog_tlk_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_dialog_tlk(tmp_path)


def test_find_dialogf_tlk_ignores_case(tmp_path):
    (tmp_path / "Dialog.tlk").write_bytes(b"")
    female = tmp_path / "dialogF.tlk"
    female.write_bytes(b"")
    assert find_dialogf_tlk(tmp_path / "Dialog.tlk") == female


# ==============================================================================
# CRE parsing
# ==============================================================================


def make_cre(version: bytes, dialog_offset: int, dialog: bytes, size: int) -> bytes:
    data = bytearray(size)
    data[0:8] = b"CRE " + version
    struct.pack_into("<ii", data, 0x0008, 274, 275)
    data[0x0237] = 1
    data[dialog_offset:dialog_offset + len(dialog)] = dialog
    return bytes(data)


def test_parse_cre_v12(tmp_path):
    path = tmp_path / "MORTE.CRE"
    path.write_bytes(make_cre(b"V1.2", 0x0370, b"DMORTE1", 0x0378))
    info = dialog_report_prepare.parse_cre(path)
    assert info is not None
    assert info.dialog_resref == "DMORTE1"
    assert (info.long_name_strref, info.short_name_strref, info.gender_byte) == (274, 275, 1)


def test_parse_cre_v10_unchanged(tmp_path):
    path = tmp_path / "AERIE.CRE"
    path.write_bytes(make_cre(b"V1.0", 0x02CC, b"AERIE", 0x02D4))
    info = dialog_report_prepare.parse_cre(path)
    assert info is not None
    assert info.dialog_resref == "AERIE"


def test_parse_cre_unsupported_version(tmp_path):
    path = tmp_path / "X.CRE"
    path.write_bytes(make_cre(b"V9.0", 0x02CC, b"X", 0x0400))
    assert dialog_report_prepare.parse_cre(path) is None


def test_parse_cre_truncated_v12(tmp_path):
    path = tmp_path / "X.CRE"
    path.write_bytes(make_cre(b"V1.2", 0x02CC, b"X", 0x0300))
    assert dialog_report_prepare.parse_cre(path) is None


# ==============================================================================
# WAVC decoding
# ==============================================================================


def test_decode_wavc_leaves_riff_untouched(tmp_path):
    path = tmp_path / "A.WAV"
    path.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    assert profiles_prepare.VoiceSampleProcessor.decode_wavc(path) is True
    assert path.read_bytes() == b"RIFF\x00\x00\x00\x00WAVE"


# ==============================================================================
# Quoted speech
# ==============================================================================


def test_spoken_text_disabled_keeps_text(isolated_cfg):
    text = 'As Pappy used to say, "A true friend is a bald-faced liar."'
    assert profiles_prepare.spoken_text(text) == text


def test_spoken_text_extracts_quoted_speech(isolated_cfg):
    isolated_cfg.QUOTED_SPEECH = True
    text = 'He searches your face. "Hail... traveler." His voice is quiet.'
    assert profiles_prepare.spoken_text(text) == "Hail... traveler."


def test_spoken_text_joins_segments_and_strips_emphasis(isolated_cfg):
    isolated_cfg.QUOTED_SPEECH = True
    text = '*"Yesssss..."* The creature floats. *"Long have I ssslept..."*'
    assert profiles_prepare.spoken_text(text) == "Yesssss... Long have I ssslept..."


def test_spoken_text_unquoted_line(isolated_cfg):
    isolated_cfg.QUOTED_SPEECH = True
    assert profiles_prepare.spoken_text("I donnae *trust* her.") == "I donnae trust her."
