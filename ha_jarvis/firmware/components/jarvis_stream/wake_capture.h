#pragma once
#include <cstddef>
#include <cstdint>
#include <string>
#include <utility>

namespace esphome::jarvis_stream {
struct WakeCaptureArm {
  static constexpr uint32_t MAX_DURATION_MS = 120000;
  static constexpr uint32_t MAX_WAIT_MS = 120000;

  std::string id;
  uint32_t duration_ms{0}, deadline_ms{0}, epoch{0};

  bool pending() const { return !id.empty(); }
  bool expired(uint32_t now) const { return static_cast<int32_t>(now - deadline_ms) >= 0; }
  bool can_fire(const std::string &word, uint32_t now, uint32_t current_epoch) const {
    return pending() && word == "Hey Jarvis" && epoch == current_epoch && !expired(now);
  }
  WakeCaptureArm take() { return std::exchange(*this, WakeCaptureArm{}); }
};

// The shared microphone is already running for the local wake engine. Drop
// the part of a callback estimated to precede the trigger; never buffer it.
constexpr size_t wake_capture_sample_offset(size_t samples, uint32_t callback_ms, uint32_t wake_ms) {
  const auto elapsed_ms = static_cast<int32_t>(callback_ms - wake_ms);
  if (elapsed_ms <= 0) return samples;
  constexpr size_t SAMPLES_PER_MS = 16;  // 16 kHz mono
  const auto elapsed = static_cast<size_t>(elapsed_ms);
  return elapsed > samples / SAMPLES_PER_MS ? 0 : samples - elapsed * SAMPLES_PER_MS;
}
}  // namespace esphome::jarvis_stream
