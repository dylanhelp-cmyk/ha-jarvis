#pragma once
#include <cstdint>

namespace esphome::jarvis_stream {
// Used only by the network worker. The ESPHome loop never executes these actions.
enum class NetworkAction { WAIT, INITIALIZE, START, STOP, DESTROY, EXIT };
class NetworkLifecycle {
 public:
  NetworkAction next(bool network_ready, bool reset_requested, bool shutdown, uint32_t now) const {
    if (shutdown) {
      if (running_) return NetworkAction::STOP;
      return initialized_ ? NetworkAction::DESTROY : NetworkAction::EXIT;
    }
    if (running_) return reset_requested ? NetworkAction::STOP : NetworkAction::WAIT;
    if (!network_ready || static_cast<int32_t>(now - retry_at_) < 0) return NetworkAction::WAIT;
    return initialized_ ? NetworkAction::START : NetworkAction::INITIALIZE;
  }
  void initialized(bool success, uint32_t now) {
    initialized_ = success;
    if (!success) retry_at_ = now + RETRY_MS;
  }
  void started(bool success, uint32_t now) {
    running_ = success;
    if (!success) retry_at_ = now + RETRY_MS;
  }
  void stopped(uint32_t now) { running_ = false; retry_at_ = now + RETRY_MS; }
  void destroyed() { initialized_ = false; }
  static constexpr uint32_t RETRY_MS = 5000;

 private:
  bool initialized_{false}, running_{false};
  uint32_t retry_at_{0};
};
}  // namespace esphome::jarvis_stream
