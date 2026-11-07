#pragma once
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <utility>
#include "protocol.h"

namespace esphome::jarvis_stream {
// Mutated only on the ESPHome loop. A failed upload cannot change the committed clip.
class AckCache {
 public:
  static constexpr uint32_t MAX_FRAMES = 48000;
  using Pcm = std::unique_ptr<uint8_t, decltype(&std::free)>;

  static bool valid_manifest(uint32_t generation, const std::string &id, uint32_t frames) {
    return generation != 0 && frames != 0 && frames <= MAX_FRAMES && id.size() == 64 &&
           id.find_first_not_of("0123456789abcdef") == std::string::npos;
  }

  void begin(uint32_t generation, std::string id, uint32_t frames, Pcm pcm) {
    staging_ = {std::move(pcm), std::move(id), frames};
    generation_ = generation;
    received_ = sequence_ = 0;
  }

  bool append(const PlaybackHeader &header, const uint8_t *pcm) {
    if (!generation_ || header.generation != generation_ || header.sequence != sequence_ ||
        header.first_sample != received_ || !header.samples || header.samples > MAX_PLAYBACK_FRAMES ||
        header.samples > staging_.frames - received_) return false;
    std::memcpy(staging_.pcm.get() + received_ * PLAYBACK_FRAME_BYTES, pcm, header.samples * PLAYBACK_FRAME_BYTES);
    received_ += header.samples;
    ++sequence_;
    return true;
  }

  bool commit() {
    if (!generation_ || received_ != staging_.frames) return false;
    committed_ = std::move(staging_);
    abort();
    return true;
  }

  void abort() {
    staging_ = {};
    generation_ = 0;
  }

  uint32_t generation() const { return generation_; }
  uint32_t received() const { return received_; }
  uint32_t frames() const { return committed_.frames; }
  const std::string &id() const { return committed_.id; }
  const uint8_t *pcm() const { return committed_.pcm.get(); }

 private:
  struct Clip {
    Pcm pcm{nullptr, std::free};
    std::string id;
    uint32_t frames{0};
  };
  Clip committed_, staging_;
  uint32_t generation_{0}, received_{0}, sequence_{0};
};
}  // namespace esphome::jarvis_stream
