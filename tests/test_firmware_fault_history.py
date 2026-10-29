"""Execute production RAM fault recording/serialization offline; no device I/O."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "ha_jarvis/firmware/components/jarvis_stream"
ARDUINO_JSON = ROOT / "ha_jarvis/firmware/.esphome/build/jarvis-voice-pe/managed_components/bblanchon__arduinojson/src"


class FirmwareFaultHistoryTests(unittest.TestCase):
    def compile_and_run(self, source, *includes):
        compiler = shutil.which("g++") or shutil.which("clang++")
        command = [compiler] if compiler else ([sys.executable, "-m", "ziglang", "c++"]
                    if importlib.util.find_spec("ziglang") else None)
        if command is None:
            self.skipTest("A host C++ compiler is required for firmware fault regressions")
        with tempfile.TemporaryDirectory(prefix="jarvis-fault-test-") as folder:
            cpp, exe = Path(folder) / "fault.cpp", Path(folder) / "fault.exe"
            cpp.write_text(source)
            subprocess.run([*command, "-std=c++17", "-I", str(COMPONENT),
                            *[arg for path in includes for arg in ("-I", str(path))],
                            str(cpp), "-o", str(exe)], check=True, capture_output=True, timeout=180)
            result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout

    def test_first_cause_survives_cascade_reconnect_and_concurrent_snapshots(self):
        output = self.compile_and_run(r'''
#include <cassert>
#include <cstdio>
#include <mutex>
#include <thread>
#include "fault_history.h"
using namespace esphome::jarvis_stream;
struct Mutex { std::mutex mutex; void enter() { mutex.lock(); } void exit() { mutex.unlock(); } };
int main() {
  FaultHistory<Mutex> history;
  assert(history.snapshot().root.sequence == 0);
  history.record(FaultReason::PCM_SEND_FAILED, 34000, 1, 7);
  history.record(FaultReason::WEBSOCKET_DISCONNECTED, 34001, 1, 7);
  history.record(FaultReason::WEBSOCKET_UNEXPECTED_FINISH, 34002, 1, 8);
  auto state = history.snapshot();
  assert(state.root.reason == FaultReason::PCM_SEND_FAILED && state.root.capture_generation == 7);
  assert(state.root.sequence == 1 && state.last.sequence == 3 && state.last.capture_generation == 8);
  // Hello/status snapshots after reconnect do not clear the preceding root.
  assert(history.snapshot().root.connection_generation == 1);
  FaultDetails details; details.has_error = true; details.has_transport_error = true;
  details.socket_errno = 104; details.esp_tls_error = 0x8001; details.ws_error = 1;
  history.record(FaultReason::WEBSOCKET_ERROR, UINT32_MAX, 2, 9, details);
  history.record(FaultReason::WEBSOCKET_CLOSED, 3, 2, 10);
  state = history.snapshot();
  assert(state.root.reason == FaultReason::WEBSOCKET_ERROR && state.root.uptime_ms == UINT32_MAX);
  assert(state.root.details.socket_errno == 104 && state.root.details.esp_tls_error == 0x8001);
  assert(!state.last.details.has_error && !state.last.details.has_transport_error);
  assert(state.counts[static_cast<size_t>(FaultReason::PCM_SEND_FAILED)] == 1);
  // Repeated callbacks across tasks cannot tear records, lose counters or overwrite the first cause.
  auto writer = [&history](FaultReason reason) {
    for (unsigned i = 0; i < 4000; ++i) history.record(reason, i, 2, 11);
  };
  std::thread a(writer, FaultReason::CONTROL_QUEUE_FULL);
  std::thread b(writer, FaultReason::OUTGOING_QUEUE_FULL);
  for (unsigned i = 0; i < 4000; ++i) {
    const auto snapshot = history.snapshot();
    uint32_t count = 0; for (auto value : snapshot.counts) count += value;
    assert(snapshot.sequence == count && snapshot.last.sequence == snapshot.sequence);
    assert(snapshot.root.sequence == 4 && snapshot.root.capture_generation == 9);
  }
  a.join(); b.join();
  state = history.snapshot();
  assert(state.sequence == 8005);
  assert(state.counts[static_cast<size_t>(FaultReason::CONTROL_QUEUE_FULL)] == 4000);
  assert(state.counts[static_cast<size_t>(FaultReason::OUTGOING_QUEUE_FULL)] == 4000);
  history.record(FaultReason::WIFI_LOST, 5000, 3, 12);
  // A send that began before reconnect may return afterwards. Its old identity
  // remains useful evidence, but must not replace the newer incident's root.
  history.record(FaultReason::PCM_SEND_FAILED, 5001, 2, 11);
  state = history.snapshot();
  assert(state.root.connection_generation == 3 && state.root.reason == FaultReason::WIFI_LOST);
  assert(state.last.connection_generation == 2 && state.last.capture_generation == 11);
  std::puts("causal ordering, reconnect retention, uptime wrap and concurrent snapshots passed");
}
''')
        self.assertIn("concurrent snapshots passed", output)

    def test_send_error_cascade_retains_middle_tcp_event_without_rewriting_root(self):
        output = self.compile_and_run(r'''
#include <cassert>
#include <cstdio>
#include <mutex>
#include "fault_history.h"
using namespace esphome::jarvis_stream;
struct Mutex { std::mutex mutex; void enter() { mutex.lock(); } void exit() { mutex.unlock(); } };
int main() {
  FaultHistory<Mutex> history;
  FaultDetails generic; generic.has_error = true;
  history.record(FaultReason::WEBSOCKET_ERROR, 34000, 2, 3, generic);
  FaultDetails tcp; tcp.has_error = tcp.has_transport_error = true;
  tcp.ws_error = 1; tcp.socket_errno = 11; tcp.esp_tls_error = 0x8001;
  history.record(FaultReason::WEBSOCKET_DISCONNECTED, 34000, 2, 3, tcp);
  history.record(FaultReason::PCM_SEND_FAILED, 34001, 2, 3);
  auto state = history.snapshot();
  assert(state.root.sequence == 1 && state.root.details.ws_error == 0);
  assert(!state.root.details.has_transport_error);
  assert(state.transport.sequence == 2 && state.transport.details.socket_errno == 11);
  assert(state.transport.connection_generation == 2 && state.transport.capture_generation == 3);
  assert(state.last.sequence == 3 && state.last.reason == FaultReason::PCM_SEND_FAILED);
  // Reconnect snapshots retain the first meaningful SDK event unchanged.
  assert(history.snapshot().transport.sequence == 2);
  history.record(FaultReason::WEBSOCKET_DISCONNECTED, 34002, 2, 4, tcp);
  assert(history.snapshot().transport.capture_generation == 3);
  // A later incident clears the old supplement, and late old callbacks cannot
  // smuggle its transport diagnostics into the new connection's incident.
  history.record(FaultReason::WEBSOCKET_ERROR, 50000, 3, 5, generic);
  history.record(FaultReason::WEBSOCKET_DISCONNECTED, 50001, 2, 4, tcp);
  assert(history.snapshot().transport.sequence == 0);
  history.record(FaultReason::WEBSOCKET_DISCONNECTED, 50002, 3, 5, tcp);
  assert(history.snapshot().transport.connection_generation == 3);
  std::puts("generic ERROR, TCP DISCONNECTED, PCM failure retention passed");
}
''')
        self.assertIn("PCM failure retention passed", output)

    @unittest.skipUnless((ARDUINO_JSON / "ArduinoJson.h").is_file(), "Cached pinned ArduinoJson required")
    def test_production_json_bounds_empty_faults_and_numeric_details(self):
        output = self.compile_and_run(r'''
#include <cassert>
#include <climits>
#include <cstdio>
#include <string>
#include "fault_diagnostics.h"
using namespace esphome::jarvis_stream;
int main() {
  JsonDocument document;
  auto hello = document.to<JsonObject>();
  FaultSnapshot state;
  write_fault_diagnostics(hello["diagnostics"].to<JsonObject>(), state, "0123456789abcdef", 10, 1, 0);
  assert(hello["diagnostics"]["root_fault"].isNull());
  assert(hello["diagnostics"]["last_fault"].isNull());
  assert(hello["diagnostics"]["transport_fault"].isNull());
  assert(hello["diagnostics"]["fault_counts"]["wifi_lost"] == 0);
  state.sequence = UINT32_MAX; state.counts.fill(UINT32_MAX);
  FaultDetails details;
  details.has_error = details.has_transport_error = true;
  details.ws_error = details.esp_tls_error = details.esp_tls_stack_error = details.esp_tls_flags = INT_MIN;
  details.socket_errno = details.http_status = details.ws_close_code = INT_MIN;
  state.root = {UINT32_MAX, UINT32_MAX, UINT32_MAX, UINT32_MAX, FaultReason::WEBSOCKET_DISCONNECTED, details};
  state.last = state.root;
  state.transport = state.root;
  write_fault_diagnostics(hello["diagnostics"].to<JsonObject>(), state, "ffffffffffffffff", UINT32_MAX, UINT32_MAX, UINT32_MAX);
  assert(hello["diagnostics"]["transport_fault"].isNull());
  // The largest valid three-record state has a generic root, a distinct first
  // TCP event, and a later full TCP event as last. A full TCP root is de-duped.
  state.root.details.has_transport_error = false;
  state.root.sequence = UINT32_MAX - 2;
  state.transport.sequence = UINT32_MAX - 1;
  hello["type"] = "hello"; hello["protocol"] = 1;
  // Exactly the validated 350-byte identity budget, including JSON escaping.
  hello["device_id"] = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd";
  hello["token"] = std::string(253, 't') + std::string(3, '"');
  hello["sample_rate"] = 16000; hello["channels"] = 1;
  hello["firmware"] = "0.1.5"; hello["firmware_project"] = "jarvis.voice_pe_testbench";
  hello["xmos_version"] = "1.3.1"; hello["muted"] = false; hello["raw_stage"] = "NONE";
  write_fault_diagnostics(hello["diagnostics"].to<JsonObject>(), state, "ffffffffffffffff", UINT32_MAX, UINT32_MAX, UINT32_MAX);
  assert(!hello["diagnostics"]["root_fault"].containsKey("errno"));
  assert(hello["diagnostics"]["transport_fault"]["errno"] == INT_MIN);
  assert(hello["diagnostics"]["last_fault"]["errno"] == INT_MIN);
  std::string wire; serializeJson(document, wire);
  assert(wire.size() < OUTGOING_LIMIT);
  std::printf("worst-case hello: %u bytes; ", unsigned(wire.size()));
  document.clear();
  state.root.details = {}; state.last.details = {}; state.transport = {};
  write_fault_diagnostics(document.to<JsonObject>(), state, "ffffffffffffffff", 20, 3, 12);
  assert(!document["root_fault"].containsKey("errno"));
  assert(!document["root_fault"].containsKey("ws_error"));
  assert(!document["root_fault"].containsKey("ws_close_code"));
  wire.clear(); serializeJson(document, wire);
  assert(wire.find("token") == std::string::npos && wire.find("device_id") == std::string::npos);
  std::puts("bounded JSON and absent diagnostics passed");
}
'''.replace("OUTGOING_LIMIT", self.outgoing_limit()), ARDUINO_JSON)
        self.assertIn("bounded JSON and absent diagnostics passed", output)

    @unittest.skipUnless(importlib.util.find_spec("esphome"), "Optional ESPHome development dependency")
    def test_identity_budget_handles_utf8_and_escaped_strings_at_boundary(self):
        spec = importlib.util.spec_from_file_location("jarvis_fault_schema", COMPONENT / "__init__.py")
        component = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(component)
        boundary = {"device_id": "d" * 64, "token": "t" * 253 + '"' * 3}
        self.assertEqual(len(json.dumps(boundary, ensure_ascii=False, separators=(",", ":")).encode("utf-8")), 350)
        self.assertIs(component.hello_identity_budget(boundary), boundary)
        with self.assertRaises(Exception):
            component.hello_identity_budget({**boundary, "token": boundary["token"] + "t"})
        self.assertIsNotNone(component.hello_identity_budget({"device_id": "d" * 64, "token": "x" * 256}))
        # Count UTF-8 bytes rather than Python characters or ASCII-only JSON.
        component.hello_identity_budget({"device_id": "é" * 32, "token": "t" * 256})
        with self.assertRaises(Exception):
            component.hello_identity_budget({"device_id": "😀" * 64, "token": "t" * 256})

    @staticmethod
    def outgoing_limit():
        import re
        return re.search(r"struct Outgoing .*?char text\[(\d+)\]", (COMPONENT / "jarvis_stream.h").read_text()).group(1)


if __name__ == "__main__":
    unittest.main()
