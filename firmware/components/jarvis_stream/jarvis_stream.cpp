#include "jarvis_stream.h"
#include <algorithm>
#include <cstring>
#include "esp_timer.h"
#include "esphome/components/json/json_util.h"
#include "esphome/core/log.h"

namespace esphome::jarvis_stream {
static const char *const TAG = "jarvis_stream";
uint32_t JarvisStream::now_ms_() { return static_cast<uint32_t>(esp_timer_get_time() / 1000); }

void JarvisStream::setup() {
  audio_queue_ = xQueueCreate(16, sizeof(AudioPacket));
  control_queue_ = xQueueCreate(8, sizeof(Control));
  if (!audio_queue_ || !control_queue_) { mark_failed(); return; }
  mic_->add_data_callback([this](const std::vector<uint8_t> &data) { microphone_data_(data); });
  esp_websocket_client_config_t config{};
  config.uri = server_url_.c_str();
  config.disable_auto_reconnect = true;
  config.task_stack = 6144;
  config.buffer_size = 1024;
  config.network_timeout_ms = 1000;
  config.ping_interval_sec = 10;
  client_ = esp_websocket_client_init(&config);
  if (!client_ || esp_websocket_register_events(client_, WEBSOCKET_EVENT_ANY, websocket_event, this) != ESP_OK) {
    mark_failed(); return;
  }
  // The loop initiates connections; no audio is sent until authenticated and explicitly commanded.
  last_connect_attempt_ = now_ms_() - 5000;
}

void JarvisStream::websocket_event(void *arg, esp_event_base_t, int32_t event, void *data) {
  static_cast<JarvisStream *>(arg)->receive_event_(event, static_cast<esp_websocket_event_data_t *>(data));
}

void JarvisStream::receive_event_(int32_t event, esp_websocket_event_data_t *data) {
  Control control{};
  if (event == WEBSOCKET_EVENT_CONNECTED) {
    control.epoch = ++epoch_;
    connected_ = true;
    authenticated_ = false;
    active_ = false;
    incoming_.clear();
    control.kind = 1;
  } else if (event == WEBSOCKET_EVENT_DISCONNECTED || event == WEBSOCKET_EVENT_CLOSED || event == WEBSOCKET_EVENT_ERROR) {
    connected_ = false;
    authenticated_ = false;
    active_ = false;  // privacy gate closes from the network task immediately
    transport_fault_ = true;
    ++epoch_;
    return;
  } else if (event == WEBSOCKET_EVENT_DATA) {
    // Only complete bounded text messages are control input. Handle transport chunking.
    if (data->op_code == 0x9 || data->op_code == 0xA) return;
    if (data->op_code != 0x1 || data->payload_len >= static_cast<int>(sizeof(control.text)) ||
        data->payload_len < 0 || data->data_len < 0 || data->payload_offset < 0) {
      transport_fault_ = true; active_ = false; return;
    }
    if (data->payload_offset == 0) incoming_.clear();
    if (static_cast<size_t>(data->payload_offset) != incoming_.size()) { transport_fault_ = true; active_ = false; return; }
    incoming_.append(data->data_ptr, data->data_len);
    if (incoming_.size() != static_cast<size_t>(data->payload_len)) return;
    if (!data->fin) { transport_fault_ = true; active_ = false; return; }
    control.kind = 2;
    control.epoch = epoch_;
    std::memcpy(control.text, incoming_.data(), incoming_.size());
  } else return;
  if (xQueueSend(control_queue_, &control, 0) != pdTRUE) { transport_fault_ = true; active_ = false; }
}

bool JarvisStream::send_json_(const std::string &text) {
  if (!connected_ || !client_) return false;
  int sent = esp_websocket_client_send_text(client_, text.c_str(), text.size(), pdMS_TO_TICKS(100));
  if (sent != static_cast<int>(text.size())) { transport_fault_ = true; active_ = false; return false; }
  return true;
}

bool JarvisStream::verify_hardware_() {
  // Read the actual running version and raw output selector, rather than assuming a successful update.
  const uint8_t version_request[] = {240, uint8_t(88 | 0x80), 4};
  uint8_t version[4]{};
  if (write(version_request, sizeof(version_request)) != i2c::ERROR_OK ||
      read(version, sizeof(version)) != i2c::ERROR_OK || version[0] != 0) return false;
  xmos_version_ = std::to_string(version[1]) + "." + std::to_string(version[2]) + "." + std::to_string(version[3]);
  if (version[1] != 1 || version[2] != 3 || version[3] != 1) return false;
  const uint8_t stage_request[] = {241, uint8_t(0x30 | 0x80), 2};
  uint8_t stage[2]{};
  return write(stage_request, sizeof(stage_request)) == i2c::ERROR_OK &&
         read(stage, sizeof(stage)) == i2c::ERROR_OK && stage[0] == 0 && stage[1] == 0;  // NONE
}

void JarvisStream::set_muted(bool muted) {
  muted_ = muted;
  if (muted) active_ = false;
}

void JarvisStream::send_state_() {
  send_json_(json::build_json([this](JsonObject root) {
    root["type"] = "status";
    root["muted"] = muted_.load();
    root["capturing"] = active_.load();
    root["raw_ready"] = hardware_ready_;
    root["xmos_version"] = xmos_version_;
  }));
}

void JarvisStream::command_(const char *text) {
  json::parse_json(std::string(text), [this](JsonObject root) {
    const std::string type = root["type"] | "";
    if (type == "hello_ok") { authenticated_ = true; send_state_(); return true; }
    if (!authenticated_) return false;
    if (type == "clock_sync") {
      const uint32_t stamp = now_ms_();
      send_json_(json::build_json([&](JsonObject out) {
        out["type"] = "clock_sync"; out["id"] = root["id"];
        out["server_time_ms"] = root["server_time_ms"]; out["device_time_ms"] = stamp;
      }));
    } else if (type == "stop_capture") {
      const std::string id = root["capture_id"] | "";
      if (id == capture_id_) stop_capture_("requested");
    } else if (type == "start_capture") {
      const std::string id = root["capture_id"] | "";
      const uint32_t duration = root["duration_ms"] | uint32_t(0);
      const char *error = nullptr;
      if (id.empty() || id.size() > 128 || duration == 0 || duration > 120000) error = "invalid_capture";
      else if (!capture_id_.empty()) error = "already_capturing";
      else if (muted_) error = "muted";
      else if (!(hardware_ready_ = verify_hardware_())) error = "raw_stage_unverified";
      else if (!mic_->is_running()) error = "microphone_not_running";
      if (error) {
        send_json_(json::build_json([&](JsonObject out) {
          out["type"] = "error"; out["capture_id"] = id; out["reason"] = error; out["message"] = error;
        }));
        return false;
      }
      xQueueReset(audio_queue_);
      capture_id_ = id;
      dropped_ = 0;
      portENTER_CRITICAL(&capture_lock_);
      ++generation_; sample_counter_ = 0; sequence_ = 0; deadline_ = now_ms_() + duration;
      portEXIT_CRITICAL(&capture_lock_);
      // Send acknowledgement before enabling any microphone upload.
      if (send_json_(json::build_json([&](JsonObject out) {
        out["type"] = "capture_started"; out["capture_id"] = id;
        out["first_sample"] = 0; out["device_time_ms"] = now_ms_();
        out["timing"] = "estimated_callback_minus_block_duration";
      }))) active_ = true;
      else stop_capture_("transport_error");
    }
    return true;
  });
}

void JarvisStream::microphone_data_(const std::vector<uint8_t> &data) {
  if (!active_ || muted_ || !authenticated_ || !connected_) return;
  // MicrophoneSource supplies 16-bit, mono, gain=1. Never block its high-priority task on Wi-Fi.
  const size_t samples = data.size() / 2;
  const uint32_t callback_ms = now_ms_();
  for (size_t offset = 0; offset < samples; offset += MAX_SAMPLES) {
    AudioPacket packet{};
    packet.samples = std::min(MAX_SAMPLES, samples - offset);
    uint64_t first_sample;
    uint32_t sequence;
    portENTER_CRITICAL(&capture_lock_);
    if (!active_ || muted_ || static_cast<int32_t>(callback_ms - deadline_) >= 0) {
      portEXIT_CRITICAL(&capture_lock_); return;
    }
    packet.generation = generation_;
    first_sample = sample_counter_; sample_counter_ += packet.samples;
    sequence = sequence_++;
    portEXIT_CRITICAL(&capture_lock_);
    const uint32_t stamp = callback_ms - static_cast<uint32_t>((samples - offset) * 1000 / 16000);
    encode_header(packet.wire, packet.samples, sequence, first_sample, stamp);
    std::memcpy(packet.wire + HEADER_BYTES, data.data() + offset * 2, packet.samples * 2);
    if (xQueueSend(audio_queue_, &packet, 0) != pdTRUE) ++dropped_;
  }
}

void JarvisStream::stop_capture_(const char *reason) {
  active_ = false;
  if (audio_queue_) xQueueReset(audio_queue_);
  if (capture_id_.empty()) return;
  uint64_t count;
  portENTER_CRITICAL(&capture_lock_);
  ++generation_; count = sample_counter_;
  portEXIT_CRITICAL(&capture_lock_);
  send_json_(json::build_json([&](JsonObject out) {
    out["type"] = "capture_stopped"; out["capture_id"] = capture_id_;
    out["reason"] = reason; out["samples_seen"] = count; out["dropped_frames"] = dropped_.load();
  }));
  capture_id_.clear();
}

void JarvisStream::wake_word(const std::string &word) {
  if (muted_ || !authenticated_) return;
  send_json_(json::build_json([&](JsonObject out) {
    out["type"] = "wake_word"; out["wake_word"] = word; out["device_time_ms"] = now_ms_();
    out["capture_id"] = capture_id_;
  }));
  // Wake is an event only: it cannot arm streaming or run a Home Assistant pipeline.
}

void JarvisStream::loop() {
  const uint32_t now = now_ms_();
  if (transport_fault_.exchange(false)) {
    stop_capture_("disconnected"); authenticated_ = false; connected_ = false;
    esp_websocket_client_stop(client_);
    xQueueReset(control_queue_);
    last_connect_attempt_ = now;
  }
  if (!connected_ && now - last_connect_attempt_ >= 5000) {
    last_connect_attempt_ = now;
    // stop() also reaps a completed connection task before restart.
    esp_websocket_client_stop(client_);
    esp_websocket_client_start(client_);
  }
  if (now - last_hardware_check_ >= 5000 && capture_id_.empty()) {
    last_hardware_check_ = now;
    const bool ready = verify_hardware_();
    if (ready != hardware_ready_) {
      hardware_ready_ = ready;
      if (authenticated_) send_state_();
    }
  }
  Control control{};
  for (int i = 0; i < 8 && xQueueReceive(control_queue_, &control, 0) == pdTRUE; ++i) {
    if (control.epoch != epoch_) continue;
    if (control.kind == 1) {
      stop_capture_("reconnected");
      send_json_(json::build_json([this](JsonObject out) {
        out["type"] = "hello"; out["protocol"] = 1; out["device_id"] = device_id_;
        out["token"] = token_; out["sample_rate"] = 16000; out["channels"] = 1;
        out["firmware"] = "jarvis-testbench-1"; out["xmos_version"] = xmos_version_;
        out["muted"] = muted_.load(); out["raw_stage"] = "NONE";
      }));
    } else command_(control.text);
  }
  if (muted_ != previous_mute_) { previous_mute_ = muted_; if (authenticated_) send_state_(); }
  if (!capture_id_.empty() && (!active_ || muted_ || static_cast<int32_t>(now - deadline_) >= 0)) {
    stop_capture_(muted_ ? "muted" : "duration_complete");
  }
  AudioPacket packet{};
  // Bounded work; stale queued samples are never replayed after a new capture.
  for (int i = 0; i < 4 && active_ && !muted_ && xQueueReceive(audio_queue_, &packet, 0) == pdTRUE; ++i) {
    if (packet.generation != generation_ || !authenticated_ || !connected_) continue;
    const size_t bytes = HEADER_BYTES + packet.samples * 2;
    if (esp_websocket_client_send_bin(client_, reinterpret_cast<const char *>(packet.wire), bytes, pdMS_TO_TICKS(30)) != static_cast<int>(bytes)) {
      transport_fault_ = true; stop_capture_("transport_error"); break;
    }
  }
}

void JarvisStream::on_shutdown() {
  active_ = false; authenticated_ = false;
  if (client_) { esp_websocket_client_stop(client_); esp_websocket_client_destroy(client_); client_ = nullptr; }
}
}  // namespace esphome::jarvis_stream
