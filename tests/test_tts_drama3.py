"""
Unit tests for libs/tts_drama3.py: device enumeration, resolution, and worker invocation.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from libs.tts_drama3 import resolve_device, get_speech_devices, Drama3Worker


MOCK_DEVICES = [
    {
        "name": "Vulkan0",
        "description": "NVIDIA GeForce GTX 1660 SUPER",
        "kind": "gpu",
        "memory_total": 6480199680,
        "memory_free": 5662310400,
    },
    {
        "name": "Vulkan1",
        "description": "NVIDIA GeForce RTX 4060 Ti",
        "kind": "gpu",
        "memory_total": 16889413632,
        "memory_free": 16075075584,
    },
    {
        "name": "CPU",
        "description": "13th Gen Intel(R) Core(TM) i7-13700F",
        "kind": "cpu",
        "memory_total": 34133311488,
        "memory_free": 19436343296,
    },
]


class TestResolveDevice:
    def test_live_system_resolves_4060_ti(self):
        # On this machine with a 4060 Ti and 1660 SUPER, default resolution targets 4060 Ti
        dev = resolve_device(None)
        assert dev == "Vulkan1"

    def test_mock_resolve_keywords(self):
        with patch("libs.tts_drama3.get_speech_devices", return_value=MOCK_DEVICES):
            assert resolve_device("auto") == "auto"
            assert resolve_device("cpu") == "cpu"
            assert resolve_device("gpu") == "gpu"

    def test_mock_resolve_exact_name(self):
        with patch("libs.tts_drama3.get_speech_devices", return_value=MOCK_DEVICES):
            assert resolve_device("Vulkan0") == "Vulkan0"
            assert resolve_device("vulkan0") == "Vulkan0"
            assert resolve_device("Vulkan1") == "Vulkan1"
            assert resolve_device("vulkan1") == "Vulkan1"

    def test_mock_resolve_by_description_substring(self):
        with patch("libs.tts_drama3.get_speech_devices", return_value=MOCK_DEVICES):
            assert resolve_device("4060") == "Vulkan1"
            assert resolve_device("4060 Ti") == "Vulkan1"
            assert resolve_device("RTX 4060 Ti") == "Vulkan1"
            assert resolve_device("1660") == "Vulkan0"
            assert resolve_device("GTX 1660 SUPER") == "Vulkan0"

    def test_mock_resolve_default_targets_4060(self):
        with patch("libs.tts_drama3.get_speech_devices", return_value=MOCK_DEVICES), \
             patch.dict("os.environ", {}, clear=True):
            # When target is None and no env var is set, it defaults to '4060 Ti' -> Vulkan1
            assert resolve_device(None) == "Vulkan1"

    def test_mock_resolve_env_var_override(self):
        with patch("libs.tts_drama3.get_speech_devices", return_value=MOCK_DEVICES), \
             patch.dict("os.environ", {"DRAMA3_DEVICE": "1660"}):
            with patch("libs.tts_drama3.DRAMA3_DEVICE", "1660"):
                assert resolve_device(None) == "Vulkan0"

    def test_mock_resolve_fallback_to_largest_gpu(self):
        # When target does not match any name/description, falls back to GPU with largest VRAM
        mock_other = [
            {"name": "Vulkan0", "description": "Card A", "kind": "gpu", "memory_total": 8000000000},
            {"name": "Vulkan1", "description": "Card B", "kind": "gpu", "memory_total": 24000000000},
        ]
        with patch("libs.tts_drama3.get_speech_devices", return_value=mock_other):
            assert resolve_device("unmatched_model") == "Vulkan1"


class TestDrama3WorkerDeviceArg:
    @patch("subprocess.Popen")
    def test_worker_cmd_includes_device(self, mock_popen, tmp_path):
        dummy_exe = tmp_path / "speech.exe"
        dummy_exe.touch()
        dummy_model = tmp_path / "model.gguf"
        dummy_model.touch()

        mock_proc = MagicMock()
        mock_proc.stdout.readline.return_value = b'{"type":"ready","model":{"device":"Vulkan1","sample_rate":44100}}\n'
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        with patch("libs.tts_drama3.resolve_device", return_value="Vulkan1"):
            worker = Drama3Worker(model=dummy_model, exe=dummy_exe, threads=4, device="4060 Ti",
                                 log_path=tmp_path / "worker.log")
            worker.close()

        # Check command line arguments passed to subprocess.Popen
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert "--device" in cmd
        dev_idx = cmd.index("--device")
        assert cmd[dev_idx + 1] == "Vulkan1"
        assert "--threads" in cmd
        assert cmd[cmd.index("--threads") + 1] == "4"
