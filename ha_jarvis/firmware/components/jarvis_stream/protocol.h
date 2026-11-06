#pragma once
#include <cstddef>
#include <cstdint>

namespace esphome::jarvis_stream {
constexpr size_t HEADER_BYTES = 24;
constexpr size_t MAX_SAMPLES = 512;
constexpr size_t MAX_PLAYBACK_FRAMES = 480;
constexpr size_t PLAYBACK_FRAME_BYTES = 4;
constexpr size_t MAX_PLAYBACK_BYTES = HEADER_BYTES + MAX_PLAYBACK_FRAMES * PLAYBACK_FRAME_BYTES;
constexpr uint64_t get_le(const uint8_t *in, size_t bytes) {
  uint64_t value = 0;
  for (size_t i = 0; i < bytes; ++i) value |= uint64_t(in[i]) << (8 * i);
  return value;
}
struct PlaybackHeader {
  uint16_t samples;
  uint32_t generation, sequence;
  uint64_t first_sample;
};
constexpr bool decode_playback_header(const uint8_t *in, size_t size, PlaybackHeader &out) {
  if (size < HEADER_BYTES || in[0] != 'J' || in[1] != 'P' || in[2] != 'L' || in[3] != 'Y' ||
      in[4] != 1 || in[5] != 2) return false;
  out = {uint16_t(get_le(in + 6, 2)), uint32_t(get_le(in + 8, 4)),
         uint32_t(get_le(in + 12, 4)), get_le(in + 16, 8)};
  return out.generation != 0 && out.samples != 0 && out.samples <= MAX_PLAYBACK_FRAMES &&
         size == HEADER_BYTES + out.samples * PLAYBACK_FRAME_BYTES;
}
// Wire encoding is explicitly little endian, independent of struct packing.
constexpr void put_le(uint8_t *out, uint64_t value, size_t bytes) {
  for (size_t i = 0; i < bytes; ++i) out[i] = static_cast<uint8_t>(value >> (8 * i));
}
constexpr void encode_header(uint8_t *out, uint16_t samples, uint32_t sequence, uint64_t first_sample, uint32_t capture_ms) {
  out[0] = 'J'; out[1] = 'P'; out[2] = 'C'; out[3] = 'M';
  out[4] = 1; out[5] = 1;
  put_le(out + 6, samples, 2);
  put_le(out + 8, sequence, 4);
  put_le(out + 12, first_sample, 8);
  put_le(out + 20, capture_ms, 4);
}
constexpr bool header_contract_valid() {
  uint8_t actual[HEADER_BYTES]{};
  encode_header(actual, 0x1234, 0x12345678, 0x0102030405060708ULL, 0x90abcdef);
  constexpr uint8_t expected[] = {'J','P','C','M',1,1,0x34,0x12,0x78,0x56,0x34,0x12,
                                 8,7,6,5,4,3,2,1,0xef,0xcd,0xab,0x90};
  for (size_t i = 0; i < HEADER_BYTES; ++i) if (actual[i] != expected[i]) return false;
  return true;
}
static_assert(header_contract_valid(), "JPCM must match Python struct <4sBBHIQI");
constexpr bool playback_contract_valid() {
  constexpr uint8_t packet[] = {'J','P','L','Y',1,2,1,0,0x78,0x56,0x34,0x12,4,3,2,1,
                                8,7,6,5,4,3,2,1,0,0,0,0};
  PlaybackHeader parsed{};
  return decode_playback_header(packet, sizeof(packet), parsed) &&
         !decode_playback_header(packet, sizeof(packet) - 1, parsed) &&
         parsed.samples == 1 && parsed.generation == 0x12345678 && parsed.sequence == 0x01020304 &&
         parsed.first_sample == 0x0102030405060708ULL;
}
static_assert(playback_contract_valid(), "JPLY must match Python struct <4sBBHIIQ");
}  // namespace esphome::jarvis_stream
