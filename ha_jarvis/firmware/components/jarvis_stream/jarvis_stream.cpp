#include "jarvis_stream.h"
#include <algorithm>
#include <cstring>
#include "esp_timer.h"
#include "esphome/components/json/json_util.h"
#include "esphome/components/network/util.h"
#include "esphome/core/defines.h"
#include "esphome/core/log.h"

namespace esphome::jarvis_stream {
static const char *const TAG = "jarvis_stream";
uint32_t JarvisStream::now_ms_() { return static_cast<uint32_t>(esp_timer_get_time() / 1000); }

void JarvisStream::setup() {
  audio_queue_ = xQueueCreate(16, sizeof(AudioPacket));
  control_queue_ = xQueueCreate(8, sizeof(Control));
  outgoing_queue_ = xQueueCreate(8, sizeof(Outgoing));
  if (!audio_queue_ || !control_queue_ || !outgoing_queue_) { mark_failed(); return; }
  mic_->add_data_callback([this](const std::vector<uint8_t> &data) { microphone_data_(data); });
  if (xTaskCreate(network_worker, "jarvis_network", 8192, this, 2, &network_worker_handle_) != pdPASS) {
    mark_failed();
  }
}

bool JarvisStream::initialize_client_() {
  esp_websocket_client_config_t config{};
  config.uri = server_url_.c_str();
  // The library task owns ordinary network-error and clean-close retries.
  config.disable_auto_reconnect = false;
  config.enable_close_reconnect = true;
  config.reconnect_timeout_ms = NetworkLifecycle::RETRY_MS;
  config.task_stack = 6144;
  config.buffer_size = 1024;
  config.network_timeout_ms = 1000;
  config.ping_interval_sec = 10;
  client_ = esp_websocket_client_init(&config);
  if (!client_ || esp_websocket_register_events(client_, WEBSOCKET_EVENT_ANY, websocket_event, this) != ESP_OK) {
    if (client_) esp_websocket_client_destroy(client_);
    client_ = nullptr;
    return false;
  }
  return true;
}

void JarvisStream::network_worker(void *arg) {
  static_cast<JarvisStream *>(arg)->network_worker_();
}

void JarvisStream::request_transport_reset_() {
  active_ = false;
  authenticated_ = false;
  connected_ = false;
  ++epoch_;
  transport_fault_ = true;
  worker_reset_ = true;
}

void JarvisStream::network_worker_() {
  NetworkLifecycle lifecycle;
  for (;;) {
    const uint32_t now = now_ms_();
    const auto action = lifecycle.next(network_ready_, worker_reset_, shutting_down_, now);
    switch (action) {
      case NetworkAction::INITIALIZE:
        lifecycle.initialized(initialize_client_(), now);
        break;
      case NetworkAction::START:
        // At most once after a successful start. Ordinary reconnects stay in the library task.
        lifecycle.started(esp_websocket_client_start(client_) == ESP_OK, now);
        break;
      case NetworkAction::STOP:
        active_ = false; authenticated_ = false; connected_ = false;
        ++epoch_;
        // This API may wait indefinitely. Only this worker may call it; never the ESPHome loop.
        esp_websocket_client_stop(client_);
        worker_reset_ = false;  // FINISH during intentional stop is already accounted for.
        lifecycle.stopped(now_ms_());
        break;
      case NetworkAction::DESTROY:
        esp_websocket_client_destroy(client_);
        client_ = nullptr;
        lifecycle.destroyed();
        break;
      case NetworkAction::EXIT:
        network_worker_handle_ = nullptr;
        vTaskDelete(nullptr);
        return;
      case NetworkAction::WAIT:
        break;
    }
    if (!shutting_down_ && connected_) {
      Outgoing outgoing{};
      for (int i = 0; i < 8 && xQueueReceive(outgoing_queue_, &outgoing, 0) == pdTRUE; ++i) {
        if (outgoing.epoch != epoch_) continue;
        if (outgoing.capture_generation && (!active_ || outgoing.capture_generation != generation_)) continue;
        const int sent = esp_websocket_client_send_text(client_, outgoing.text, outgoing.length, pdMS_TO_TICKS(100));
        if (sent != outgoing.length) {
          ESP_LOGW(TAG, "Resetting transport: control send failed (sent=%d expected=%u)", sent,
                   static_cast<unsigned>(outgoing.length));
          request_transport_reset_();
          break;
        }
        if (outgoing.capture_generation) acknowledged_generation_ = outgoing.capture_generation;
      }
      AudioPacket packet{};
      for (int i = 0; i < 4 && active_ && !muted_ && authenticated_ && connected_ &&
                           acknowledged_generation_ == generation_ &&
                           xQueueReceive(audio_queue_, &packet, 0) == pdTRUE; ++i) {
        if (packet.generation != generation_) continue;
        const size_t bytes = HEADER_BYTES + packet.samples * 2;
        const int sent = esp_websocket_client_send_bin(client_, reinterpret_cast<const char *>(packet.wire), bytes,
                                                       pdMS_TO_TICKS(30));
        if (sent != static_cast<int>(bytes)) {
          ESP_LOGW(TAG, "Resetting transport: PCM send failed (sent=%d expected=%u)", sent,
                   static_cast<unsigned>(bytes));
          request_transport_reset_();
          break;
        }
      }
    }
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

void JarvisStream::websocket_event(void *arg, esp_event_base_t, int32_t event, void *data) {
  static_cast<JarvisStream *>(arg)->receive_event_(event, static_cast<esp_websocket_event_data_t *>(data));
}

void JarvisStream::receive_event_(int32_t event, esp_websocket_event_data_t *data) {
  if (event == WEBSOCKET_EVENT_FINISH) {
    // Covers an unexpected library-task exit; worker reaps it and retries.
    worker_reset_ = true;
    return;
  }
  if (shutting_down_) return;
  Control control{};
  if (event == WEBSOCKET_EVENT_CONNECTED) {
    control.epoch = ++epoch_;
    connected_ = true;
    authenticated_ = false;
    active_ = false;
    acknowledged_generation_ = 0;
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
      request_transport_reset_(); return;
    }
    if (data->payload_offset == 0) incoming_.clear();
    if (static_cast<size_t>(data->payload_offset) != incoming_.size()) { request_transport_reset_(); return; }
    incoming_.append(data->data_ptr, data->data_len);
    if (incoming_.size() != static_cast<size_t>(data->payload_len)) return;
    if (!data->fin) { request_transport_reset_(); return; }
    control.kind = 2;
    control.epoch = epoch_;
    std::memcpy(control.text, incoming_.data(), incoming_.size());
  } else return;
  if (xQueueSend(control_queue_, &control, 0) != pdTRUE) request_transport_reset_();
}

bool JarvisStream::send_json_(const std::string &text, bool capture_start) {
  if (!connected_ || shutting_down_) return false;
  Outgoing outgoing{};
  if (text.size() >= sizeof(outgoing.text)) { request_transport_reset_(); return false; }
  outgoing.epoch = epoch_;
  outgoing.capture_generation = capture_start ? generation_.load() : 0;
  outgoing.length = text.size();
  std::memcpy(outgoing.text, text.data(), text.size());
  if (xQueueSend(outgoing_queue_, &outgoing, 0) != pdTRUE) { request_transport_reset_(); return false; }
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
      acknowledged_generation_ = 0;
      // Arm locally, but the worker sends no PCM until this acknowledgement is actually transmitted.
      active_ = true;
      if (!send_json_(json::build_json([&](JsonObject out) {
        out["type"] = "capture_started"; out["capture_id"] = id;
        out["first_sample"] = 0; out["device_time_ms"] = now_ms_();
        out["timing"] = "estimated_callback_minus_block_duration";
      }), true)) stop_capture_("transport_error");
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
  // A newly reconnected socket must receive hello first, not the previous capture's stop event.
  if (authenticated_) {
    send_json_(json::build_json([&](JsonObject out) {
      out["type"] = "capture_stopped"; out["capture_id"] = capture_id_;
      out["reason"] = reason; out["samples_seen"] = count; out["dropped_frames"] = dropped_.load();
    }));
  }
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
  const bool network_ready = network::is_connected();
  if (!network_ready && network_ready_.exchange(false)) {
    // A brief Wi-Fi flap may leave TCP apparently alive. Force a fresh hello on recovery.
    request_transport_reset_();
  } else if (network_ready) network_ready_ = true;
  if (transport_fault_.exchange(false)) {
    stop_capture_("disconnected");
    // Preserve a newly arrived CONNECTED event; stale controls are rejected by epoch below.
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
        out["firmware"] = ESPHOME_PROJECT_VERSION; out["firmware_project"] = ESPHOME_PROJECT_NAME;
        out["xmos_version"] = xmos_version_;
        out["muted"] = muted_.load(); out["raw_stage"] = "NONE";
      }));
    } else command_(control.text);
  }
  if (muted_ != previous_mute_) { previous_mute_ = muted_; if (authenticated_) send_state_(); }
  if (!capture_id_.empty() && (!active_ || muted_ || static_cast<int32_t>(now - deadline_) >= 0)) {
    stop_capture_(muted_ ? "muted" : "duration_complete");
  }
}

void JarvisStream::on_shutdown() {
  // ESPHome shutdown/OTA must not wait for a network task, socket or DNS operation.
  active_ = false; authenticated_ = false; connected_ = false;
  network_ready_ = false; shutting_down_ = true;
}
}  // namespace esphome::jarvis_stream
