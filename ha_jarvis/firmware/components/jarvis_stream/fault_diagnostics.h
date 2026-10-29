#pragma once
#include <ArduinoJson.h>
#include "fault_history.h"
#include "pcm_send_history.h"

namespace esphome::jarvis_stream {
inline void write_pcm_send_diagnostics(JsonObject out, const PcmSendSnapshot &snapshot) {
  if (!snapshot.valid) { out["pcm_send"] = nullptr; return; }
  JsonObject value = out["pcm_send"].to<JsonObject>();
  value["connection_generation"] = snapshot.connection_generation;
  value["capture_generation"] = snapshot.capture_generation;
  value["send_count"] = snapshot.send_count;
  value["failed_send_count"] = snapshot.failed_send_count;
  value["max_success_duration_ms"] = snapshot.max_success_duration_ms;
  value["max_failed_duration_ms"] = snapshot.max_failed_duration_ms;
  value["max_queue_age_ms"] = snapshot.max_queue_age_ms;
  if (!snapshot.has_failure) { value["last_failed_send"] = nullptr; return; }
  JsonObject failed = value["last_failed_send"].to<JsonObject>();
  failed["returned_bytes"] = snapshot.last_failed_send.returned_bytes;
  failed["expected_bytes"] = snapshot.last_failed_send.expected_bytes;
  failed["duration_ms"] = snapshot.last_failed_send.duration_ms;
  failed["uptime_ms"] = snapshot.last_failed_send.uptime_ms;
}
// Called only for locally generated, epoch-matched stop controls in the sending
// worker. Refresh after its in-flight send completes, without attaching a newer
// capture's summary if another capture has already replaced the retained one.
inline size_t refresh_stop_pcm_diagnostics(JsonDocument &document, const PcmSendSnapshot &snapshot,
                                           char *output, size_t capacity) {
  if (document.isNull() || document.overflowed()) return 0;
  JsonObject root = document.as<JsonObject>();
  JsonObject diagnostics = root["diagnostics"].as<JsonObject>();
  JsonObject previous = diagnostics["pcm_send"].as<JsonObject>();
  if (snapshot.valid && previous["connection_generation"].is<uint32_t>() &&
      previous["capture_generation"].is<uint32_t>() &&
      previous["connection_generation"].as<uint32_t>() == snapshot.connection_generation &&
      previous["capture_generation"].as<uint32_t>() == snapshot.capture_generation)
    write_pcm_send_diagnostics(diagnostics, snapshot);
  const size_t length = measureJson(root);
  if (document.overflowed() || length >= capacity) return 0;
  return serializeJson(root, output, capacity) == length ? length : 0;
}
inline void write_fault_record(JsonObject parent, const char *key, const FaultRecord &record) {
    if (record.sequence == 0) { parent[key] = nullptr; return; }
    JsonObject value = parent[key].to<JsonObject>();
    value["sequence"] = record.sequence;
    value["reason"] = FAULT_NAMES[static_cast<size_t>(record.reason)];
    value["uptime_ms"] = record.uptime_ms;
    value["connection_generation"] = record.connection_generation;
    value["capture_generation"] = record.capture_generation;
    if (record.details.has_error) {
      value["ws_error"] = record.details.ws_error;
      value["http_status"] = record.details.http_status;
    }
    if (record.details.has_transport_error) {
      value["esp_tls_error"] = record.details.esp_tls_error;
      value["esp_tls_stack_error"] = record.details.esp_tls_stack_error;
      value["esp_tls_flags"] = record.details.esp_tls_flags;
      value["errno"] = record.details.socket_errno;
    }
    if (record.details.ws_close_code != 0) value["ws_close_code"] = record.details.ws_close_code;
}
inline void write_fault_diagnostics(JsonObject out, const FaultSnapshot &snapshot, const char *boot_id,
                                    uint32_t uptime_ms, uint32_t connection_generation, uint32_t capture_generation) {
  out["schema"] = 1;
  out["boot_id"] = boot_id;
  out["uptime_ms"] = uptime_ms;
  out["connection_generation"] = connection_generation;
  out["capture_generation"] = capture_generation;
  out["fault_sequence"] = snapshot.sequence;
  JsonObject counts = out["fault_counts"].to<JsonObject>();
  for (size_t i = 0; i < snapshot.counts.size(); ++i) counts[FAULT_NAMES[i]] = snapshot.counts[i];
  write_fault_record(out, "root_fault", snapshot.root);
  write_fault_record(out, "last_fault", snapshot.last);
  // Avoid serializing duplicate numeric records; root/last already export them.
  if (snapshot.transport.sequence == snapshot.root.sequence || snapshot.transport.sequence == snapshot.last.sequence)
    out["transport_fault"] = nullptr;
  else
    write_fault_record(out, "transport_fault", snapshot.transport);
}
}  // namespace esphome::jarvis_stream
