#pragma once
#include <ArduinoJson.h>
#include "fault_history.h"

namespace esphome::jarvis_stream {
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
