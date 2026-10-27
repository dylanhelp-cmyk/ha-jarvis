"""Firmware wire ABI and configuration regressions; no device is contacted."""
import importlib.util
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "ha_jarvis" / "firmware"


class FirmwareContractTests(unittest.TestCase):
    def test_actual_cpp_encoder_matches_wire_struct(self):
        compiler = shutil.which("g++") or shutil.which("clang++")
        if compiler is None:
            self.skipTest("Host C++ compiler unavailable; firmware compile also checks the ABI with static_assert")
        source = r'''
#include <cstdio>
#include "protocol.h"
int main() {
  unsigned char h[24]{};
  esphome::jarvis_stream::encode_header(h, 320, 0x12345678, 0x123456789abcdef0ULL, 0xfedcba98);
  for (unsigned char v : h) std::printf("%02x", unsigned(v));
}
'''
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            cpp, exe = tmp / "contract.cpp", tmp / "contract.exe"
            cpp.write_text(source)
            subprocess.run([compiler, "-std=c++17", "-I", str(FIRMWARE / "components/jarvis_stream"), str(cpp), "-o", str(exe)], check=True, capture_output=True)
            actual = bytes.fromhex(subprocess.check_output([str(exe)], text=True))
        self.assertEqual(actual, struct.pack("<4sBBHIQI", b"JPCM", 1, 1, 320, 0x12345678, 0x123456789abcdef0, 0xfedcba98))

    def test_hardware_and_privacy_configuration(self):
        main = (FIRMWARE / "voice-pe-testbench.yaml").read_text()
        package = (FIRMWARE / "voice-pe-package.yaml").read_text()
        hardware = (FIRMWARE / "voice-pe-hardware.yaml").read_text()
        self.assertNotIn("\nvoice_assistant:", main + package + hardware)
        self.assertIn("channel_0_stage: NONE", hardware)
        self.assertIn("channel_1_stage: NS", hardware)
        self.assertIn("correct_dc_offset: false", hardware)
        self.assertIn('version: "1.3.1"', hardware)
        self.assertIn("964635c5bf125529dab14a2472a15401", hardware)
        self.assertIn("d4e6fa43d6a1a7d342b7d4d403567e06516719de", package)
        self.assertIn("gain_factor: 1", package)
        self.assertIn("!secret jarvis_pairing_token", main)
        self.assertIn("pin: GPIO3", package)
        self.assertIn("microphone.mute: i2s_mics", package)

    def test_repository_entry_keeps_credentials_local(self):
        entry = (FIRMWARE / "voice-pe-repository.yaml").read_text()
        for name in ("voice-pe-package.yaml", "voice-pe-hardware.yaml"):
            self.assertNotIn("!secret", (FIRMWARE / name).read_text())
        self.assertIn("!secret wifi_password", entry)
        self.assertIn("!secret jarvis_pairing_token", entry)
        self.assertIn("files: [ha_jarvis/firmware/voice-pe-package.yaml]", entry)
        self.assertIn("path: ha_jarvis/firmware/components", entry)
        self.assertEqual(entry.count("refresh: 5min"), 2)
        self.assertEqual(entry.count("ref: main"), 2)
        self.assertNotIn("password:", entry.split("packages:", 1)[1])

    @unittest.skipUnless(importlib.util.find_spec("esphome"), "ESPHome is an optional firmware development dependency")
    def test_transport_url_schema_rejects_wrong_paths_and_embedded_credentials(self):
        spec = importlib.util.spec_from_file_location("jarvis_firmware_schema", FIRMWARE / "components/jarvis_stream/__init__.py")
        component = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(component)
        self.assertEqual(component.server_url("ws://192.168.1.10:8099/device"), "ws://192.168.1.10:8099/device")
        for url in ("http://host/device", "ws://host/other", "ws://user:password@host/device", "ws://host/device?token=secret", "ws:///device"):
            with self.subTest(url=url), self.assertRaises(Exception):
                component.server_url(url)
        raw = component.raw_microphone({"microphone": "i2s_mics", "channels": [0], "gain_factor": 1})
        self.assertEqual(raw["bits_per_sample"], 16)
        for override in ({"channels": [1]}, {"gain_factor": 4}, {"bits_per_sample": 32}):
            with self.subTest(override=override), self.assertRaises(Exception):
                component.raw_microphone({"microphone": "i2s_mics", "channels": [0], "gain_factor": 1, **override})


if __name__ == "__main__":
    unittest.main()
