#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>

namespace esphome::jarvis_stream {
enum class FaultReason : uint8_t {
  WIFI_LOST, CONTROL_SEND_FAILED, PCM_SEND_FAILED, WEBSOCKET_ERROR, WEBSOCKET_CLOSED,
  WEBSOCKET_DISCONNECTED, WEBSOCKET_UNEXPECTED_FINISH, MALFORMED_MESSAGE,
  CONTROL_QUEUE_FULL, OUTGOING_QUEUE_FULL, OUTGOING_MESSAGE_TOO_LARGE, COUNT
};
inline constexpr const char *FAULT_NAMES[] = {
  "wifi_lost", "control_send_failed", "pcm_send_failed", "websocket_error", "websocket_closed",
  "websocket_disconnected", "websocket_unexpected_finish", "malformed_message",
  "control_queue_full", "outgoing_queue_full", "outgoing_message_too_large"
};
struct FaultDetails {
  bool has_error{false}, has_transport_error{false};
  int ws_error{0}, esp_tls_error{0}, esp_tls_stack_error{0}, esp_tls_flags{0}, socket_errno{0}, http_status{0};
  int ws_close_code{0};
};
struct FaultRecord {
  uint32_t sequence{0}, uptime_ms{0}, connection_generation{0}, capture_generation{0};
  FaultReason reason{FaultReason::WIFI_LOST};
  FaultDetails details{};
};
struct FaultSnapshot {
  uint32_t sequence{0};
  std::array<uint32_t, static_cast<size_t>(FaultReason::COUNT)> counts{};
  FaultRecord root{}, last{};
};

// Lock must provide enter()/exit(). Production uses a tiny FreeRTOS critical
// section; host tests use a mutex. No allocation, JSON, logging or I/O under lock.
template<class Lock> class FaultHistory {
 public:
  FaultRecord record(FaultReason reason, uint32_t uptime_ms, uint32_t connection_generation,
                     uint32_t capture_generation, FaultDetails details = {}) {
    lock_.enter();
    ++state_.sequence;
    // Reserve zero for "no fault", even at the sequence wrap.
    if (state_.sequence == 0) ++state_.sequence;
    auto &count = state_.counts[static_cast<size_t>(reason)];
    if (count != std::numeric_limits<uint32_t>::max()) ++count;
    FaultRecord record{state_.sequence, uptime_ms, connection_generation, capture_generation, reason, details};
    // A reconnect does not erase evidence. Only the first fault on a different
    // connected generation replaces the preceding incident's causal event.
    if (state_.root.sequence == 0 ||
        static_cast<int32_t>(connection_generation - state_.root.connection_generation) > 0)
      state_.root = record;
    state_.last = record;
    lock_.exit();
    return record;
  }
  FaultSnapshot snapshot() {
    lock_.enter();
    const FaultSnapshot snapshot = state_;
    lock_.exit();
    return snapshot;
  }
 private:
  Lock lock_{};
  FaultSnapshot state_{};
};
}  // namespace esphome::jarvis_stream
