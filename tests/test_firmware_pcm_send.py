"""Exercise the production numeric PCM summary without a device or SDK socket."""
import unittest
import test_firmware_fault_history as helpers
ARDUINO_JSON = helpers.ARDUINO_JSON


class FirmwarePcmSendTests(unittest.TestCase):
    compile_and_run = helpers.FirmwareFaultHistoryTests.compile_and_run

    def test_capture_retention_failure_values_and_stale_send_attribution(self):
        output = self.compile_and_run(r'''
#include <cassert>
#include <cstdio>
#include <mutex>
#include <thread>
#include "pcm_send_history.h"
using namespace esphome::jarvis_stream;
struct Mutex { std::mutex m; void enter() { m.lock(); } void exit() { m.unlock(); } };
int main() {
  PcmSendHistory<Mutex> history;
  assert(!history.snapshot().valid);
  history.record(1, 1, -1, 536, 100, 3, 1000);
  assert(!history.snapshot().valid);
  history.start(2, 3);
  history.record(2, 3, 536, 536, 5, 10, 1000);
  history.record(2, 3, 536, 536, 45, 8, 1050);
  history.record(2, 3, 0, 536, 100, 20, 1200);
  auto state = history.snapshot();
  assert(state.send_count == 2 && state.failed_send_count == 1);
  assert(state.max_success_duration_ms == 45 && state.max_failed_duration_ms == 100);
  assert(state.max_queue_age_ms == 20 && state.has_failure);
  assert(state.last_failed_send.returned_bytes == 0 && state.last_failed_send.expected_bytes == 536);
  // Snapshots after stop/reconnect preserve the capture IDs; outer generation
  // changes are not inputs to this class and cannot erase or relabel them.
  assert(history.snapshot().connection_generation == 2 && history.snapshot().capture_generation == 3);
  history.record(2, 3, -1, 536, 120, 30, 1320);
  assert(history.snapshot().last_failed_send.returned_bytes == -1);
  history.record(2, 3, 100, 536, 10, 35, 1400);
  assert(history.snapshot().failed_send_count == 3);
  assert(history.snapshot().max_failed_duration_ms == 120);
  assert(history.snapshot().last_failed_send.returned_bytes == 100);
  history.start(4, 7);
  history.record(2, 3, -1, 536, 999, 999, 1500);
  state = history.snapshot();
  assert(state.send_count == 0 && state.failed_send_count == 0 && !state.has_failure);
  assert(state.max_queue_age_ms == 0 && state.capture_generation == 7);
  // Record/snapshot concurrency cannot expose a torn failure record or count.
  auto writer = [&history]() {
    for (unsigned i = 0; i < 4000; ++i) history.record(4, 7, -1, 536, i, i, i);
  };
  std::thread thread(writer);
  for (unsigned i = 0; i < 4000; ++i) {
    state = history.snapshot();
    if (state.has_failure) {
      assert(state.failed_send_count == state.last_failed_send.duration_ms + 1);
      assert(state.max_failed_duration_ms == state.max_queue_age_ms);
      assert(state.last_failed_send.uptime_ms == state.last_failed_send.duration_ms);
    }
  }
  thread.join();
  assert(history.snapshot().failed_send_count == 4000);
  // Queue age/duration subtraction is unsigned and remains valid across uptime wrap.
  const uint32_t begin = UINT32_MAX - 5, end = 4;
  assert(end - begin == 10);
  std::puts("retention, reset, stale sends, failure values and concurrency passed");
}
''')
        self.assertIn("concurrency passed", output)

    @unittest.skipUnless((ARDUINO_JSON / "ArduinoJson.h").is_file(), "Cached pinned ArduinoJson required")
    def test_production_json_distinguishes_unavailable_no_failure_and_zero_return(self):
        self.compile_and_run(r'''
#include <cassert>
#include "fault_diagnostics.h"
using namespace esphome::jarvis_stream;
int main() {
  JsonDocument document;
  auto root = document.to<JsonObject>();
  PcmSendSnapshot snapshot;
  write_pcm_send_diagnostics(root, snapshot);
  assert(root["pcm_send"].isNull());
  snapshot.valid = true;
  snapshot.connection_generation = 3; snapshot.capture_generation = 7;
  write_pcm_send_diagnostics(root, snapshot);
  assert(root["pcm_send"]["last_failed_send"].isNull());
  assert(root["pcm_send"]["send_count"] == 0);
  snapshot.has_failure = true;
  snapshot.last_failed_send = {0, 536, 100, 5000};
  write_pcm_send_diagnostics(root, snapshot);
  assert(root["pcm_send"]["last_failed_send"]["returned_bytes"].is<int>());
  assert(root["pcm_send"]["last_failed_send"]["returned_bytes"] == 0);
  assert(root["pcm_send"]["last_failed_send"]["duration_ms"] == 100);
  snapshot.last_failed_send.returned_bytes = -1;
  write_pcm_send_diagnostics(root, snapshot);
  assert(root["pcm_send"]["last_failed_send"]["returned_bytes"] == -1);
  assert(root["pcm_send"]["capture_generation"] == 7);
}
''', ARDUINO_JSON)

    @unittest.skipUnless((ARDUINO_JSON / "ArduinoJson.h").is_file(), "Cached pinned ArduinoJson required")
    def test_worker_stop_refresh_includes_completed_send_and_never_relabels_old_capture(self):
        self.compile_and_run(r'''
#include <cassert>
#include <mutex>
#include "fault_diagnostics.h"
using namespace esphome::jarvis_stream;
struct Mutex { std::mutex m; void enter() { m.lock(); } void exit() { m.unlock(); } };
int main() {
  PcmSendHistory<Mutex> history;
  history.start(2, 7);
  JsonDocument queued;
  queued["type"] = "capture_stopped"; queued["capture_id"] = "capture-a";
  write_pcm_send_diagnostics(queued["diagnostics"].to<JsonObject>(), history.snapshot());
  // Main loop queued stop while worker's SDK call had not returned yet.
  history.record(2, 7, 536, 536, 80, 20, 3000);
  char output[3072];
  auto length = refresh_stop_pcm_diagnostics(queued, history.snapshot(), output, sizeof(output));
  assert(length != 0);
  JsonDocument wire; assert(!deserializeJson(wire, output, length));
  assert(wire["diagnostics"]["pcm_send"]["send_count"] == 1);
  assert(wire["diagnostics"]["pcm_send"]["max_success_duration_ms"] == 80);
  assert(wire["type"] == "capture_stopped" && wire["capture_id"] == "capture-a");
  // A rapid next capture must not be attached to the preceding stop response.
  history.start(2, 9);
  history.record(2, 9, -1, 536, 100, 150, 3200);
  length = refresh_stop_pcm_diagnostics(queued, history.snapshot(), output, sizeof(output));
  assert(length != 0);
  assert(!deserializeJson(wire, output, length));
  assert(wire["diagnostics"]["pcm_send"]["capture_generation"] == 7);
  assert(wire["diagnostics"]["pcm_send"]["send_count"] == 1);
  assert(refresh_stop_pcm_diagnostics(queued, history.snapshot(), output, 10) == 0);
  JsonDocument invalid;
  assert(refresh_stop_pcm_diagnostics(invalid, history.snapshot(), output, sizeof(output)) == 0);
}
''', ARDUINO_JSON)
