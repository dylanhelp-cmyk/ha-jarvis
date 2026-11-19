#pragma once
#include <atomic>
#include <cstdint>
#include <limits>

namespace esphome::jarvis_stream {

class TaskStackMinimum {
 public:
  static constexpr uint32_t UNKNOWN = std::numeric_limits<uint32_t>::max();

  void observe(uint32_t free_bytes) {
    uint32_t previous = minimum_.load(std::memory_order_relaxed);
    while (free_bytes < previous &&
           !minimum_.compare_exchange_weak(previous, free_bytes, std::memory_order_relaxed)) {}
  }

  uint32_t value() const { return minimum_.load(std::memory_order_relaxed); }

 private:
  std::atomic<uint32_t> minimum_{UNKNOWN};
};

}  // namespace esphome::jarvis_stream
