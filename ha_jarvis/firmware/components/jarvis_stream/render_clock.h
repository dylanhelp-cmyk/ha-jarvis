#pragma once
#include <cstdint>

namespace esphome::jarvis_stream {

struct RenderClock {
  uint32_t frames{0};
  int64_t first_us{0}, last_us{0};
  uint64_t max_discontinuity_us{0};

  // The speaker reports only the real PCM at the front of each DMA
  // buffer. Silence inserted between buffers appears as a time gap.
  void append(uint32_t count, int64_t end_us) {
    if (!count) return;
    const int64_t start_us = end_us - static_cast<int64_t>(count) * 1000000 / 48000;
    if (!frames) {
      first_us = start_us;
    } else {
      const int64_t gap = start_us - last_us;
      const uint64_t magnitude = gap < 0 ? -gap : gap;
      if (magnitude > max_discontinuity_us) max_discontinuity_us = magnitude;
    }
    frames += count;
    last_us = end_us;
  }
};

}  // namespace esphome::jarvis_stream
