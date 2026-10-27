#pragma once
#include <cstddef>
#include <cstdint>

namespace esphome::jarvis_stream {
constexpr size_t HEADER_BYTES = 24;
constexpr size_t MAX_SAMPLES = 512;
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
}  // namespace esphome::jarvis_stream
