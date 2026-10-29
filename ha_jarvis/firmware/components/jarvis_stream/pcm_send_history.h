#pragma once
#include <algorithm>
#include <cstdint>
#include <limits>

namespace esphome::jarvis_stream {
inline constexpr uint32_t PCM_SEND_TIMEOUT_MS = 100;
struct PcmSendFailure {
  int returned_bytes{0};
  uint32_t expected_bytes{0}, duration_ms{0}, uptime_ms{0};
};
struct PcmSendSnapshot {
  bool valid{false}, has_failure{false};
  uint32_t connection_generation{0}, capture_generation{0};
  uint32_t send_count{0}, failed_send_count{0}, max_success_duration_ms{0}, max_failed_duration_ms{0};
  uint32_t max_queue_age_ms{0};
  PcmSendFailure last_failed_send{};
};
// Only small numeric copies/arithmetic under lock; never hold it across SDK I/O.
// Stop/reconnect leave the snapshot intact. A new accepted capture replaces it.
template<class Lock> class PcmSendHistory {
 public:
  void start(uint32_t connection, uint32_t capture) {
    lock_.enter();
    state_ = {};
    state_.valid = true;
    state_.connection_generation = connection;
    state_.capture_generation = capture;
    lock_.exit();
  }
  void record(uint32_t connection, uint32_t capture, int returned_bytes, uint32_t expected_bytes,
              uint32_t duration_ms, uint32_t queue_age_ms, uint32_t uptime_ms) {
    lock_.enter();
    // A blocked old send may finish after stop/new capture. Never attribute it to
    // the new capture or recreate an erased snapshot with the old identity.
    if (state_.valid && state_.connection_generation == connection && state_.capture_generation == capture) {
      state_.max_queue_age_ms = std::max(state_.max_queue_age_ms, queue_age_ms);
      if (returned_bytes >= 0 && static_cast<uint32_t>(returned_bytes) == expected_bytes) {
        increment_(state_.send_count);
        state_.max_success_duration_ms = std::max(state_.max_success_duration_ms, duration_ms);
      } else {
        increment_(state_.failed_send_count);
        state_.max_failed_duration_ms = std::max(state_.max_failed_duration_ms, duration_ms);
        state_.has_failure = true;
        state_.last_failed_send = {returned_bytes, expected_bytes, duration_ms, uptime_ms};
      }
    }
    lock_.exit();
  }
  PcmSendSnapshot snapshot() {
    lock_.enter();
    const auto snapshot = state_;
    lock_.exit();
    return snapshot;
  }
 private:
  static void increment_(uint32_t &value) { if (value != std::numeric_limits<uint32_t>::max()) ++value; }
  Lock lock_{};
  PcmSendSnapshot state_{};
};
}  // namespace esphome::jarvis_stream
