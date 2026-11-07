#include "jarvis_stream.h"
#include "fault_diagnostics.h"
#include <algorithm>
#include <cstring>
#include <cstdio>
#include "esp_heap_caps.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esphome/components/json/json_util.h"
#include "esphome/components/network/util.h"
#include "esphome/core/defines.h"
#include "esphome/core/log.h"

namespace esphome::jarvis_stream {
static const char *const TAG = "jarvis_stream";
uint32_t JarvisStream::now_ms_() { return static_cast<uint32_t>(esp_timer_get_time() / 1000); }

void JarvisStream::setup() {
  std::snprintf(boot_id_, sizeof(boot_id_), "%08lx%08lx", static_cast<unsigned long>(esp_random()),
                static_cast<unsigned long>(esp_random()));
  audio_queue_ = xQueueCreate(16, sizeof(AudioPacket));
  control_queue_ = xQueueCreate(8, sizeof(Control));
  outgoing_queue_ = xQueueCreate(8, sizeof(Outgoing));
  playback_queue_ = xQueueCreate(8, sizeof(PlaybackPacket));
  if (!audio_queue_ || !control_queue_ || !outgoing_queue_ || !playback_queue_) { mark_failed(); return; }
  speaker_->set_audio_stream_info(audio::AudioStreamInfo(32, 2, 48000));
  speaker_->add_audio_output_callback([this](uint32_t frames, int64_t end_us) {
    playback_clock_lock_.enter();
    if (!rendered_samples_) {
      const int64_t start_us = end_us - static_cast<int64_t>(frames) * 1000000 / 48000;
      rendered_start_ms_ = static_cast<uint32_t>(start_us / 1000);
    }
    rendered_samples_ += frames;
    rendered_end_ms_ = static_cast<uint32_t>(end_us / 1000);
    playback_clock_lock_.exit();
  });
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
        intentional_stop_ = true;
        active_ = false; authenticated_ = false; connected_ = false;
        ++epoch_;
        // This API may wait indefinitely. Only this worker may call it; never the ESPHome loop.
        esp_websocket_client_stop(client_);
        intentional_stop_ = false;
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
        // Stop controls are serialized after the worker's preceding PCM send
        // returns, so its acknowledgement includes that send's final metrics.
        if (outgoing.refresh_diagnostics) {
          JsonDocument document = json::parse_json(reinterpret_cast<const uint8_t *>(outgoing.text), outgoing.length);
          outgoing.length = refresh_stop_pcm_diagnostics(document, pcm_send_history_.snapshot(),
                                                         outgoing.text, sizeof(outgoing.text));
          if (outgoing.length == 0) {
            record_fault_(FaultReason::OUTGOING_MESSAGE_TOO_LARGE);
            request_transport_reset_();
            break;
          }
        }
        const uint32_t sending_connection = connection_generation_.load();
        const uint32_t sending_capture = outgoing.capture_generation ? outgoing.capture_generation : generation_.load();
        const int sent = esp_websocket_client_send_text(client_, outgoing.text, outgoing.length, pdMS_TO_TICKS(100));
        if (sent != outgoing.length) {
          record_fault_at_(FaultReason::CONTROL_SEND_FAILED, sending_connection, sending_capture);
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
        const uint32_t sending_connection = connection_generation_.load();
        const uint32_t send_started = now_ms_();
        const uint32_t queue_age = send_started - packet.enqueued_ms;
        const int sent = esp_websocket_client_send_bin(client_, reinterpret_cast<const char *>(packet.wire), bytes,
                                                       pdMS_TO_TICKS(PCM_SEND_TIMEOUT_MS));
        const uint32_t send_finished = now_ms_();
        pcm_send_history_.record(sending_connection, packet.generation, sent, bytes,
                                 send_finished - send_started, queue_age, send_finished);
        if (sent != static_cast<int>(bytes)) {
          record_fault_at_(FaultReason::PCM_SEND_FAILED, sending_connection, packet.generation);
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
    if (!intentional_stop_ && !shutting_down_) record_fault_(FaultReason::WEBSOCKET_UNEXPECTED_FINISH);
    // Covers an unexpected library-task exit; worker reaps it and retries.
    worker_reset_ = true;
    return;
  }
  if (shutting_down_) return;
  Control control{};
  if (event == WEBSOCKET_EVENT_CONNECTED) {
    ++connection_generation_;
    control.epoch = ++epoch_;
    connected_ = true;
    authenticated_ = false;
    active_ = false;
    acknowledged_generation_ = 0;
    incoming_.clear();
    control.kind = 1;
  } else if (event == WEBSOCKET_EVENT_DISCONNECTED || event == WEBSOCKET_EVENT_CLOSED || event == WEBSOCKET_EVENT_ERROR) {
    if (!intentional_stop_) {
      record_fault_(event == WEBSOCKET_EVENT_ERROR ? FaultReason::WEBSOCKET_ERROR :
                    event == WEBSOCKET_EVENT_CLOSED ? FaultReason::WEBSOCKET_CLOSED :
                    FaultReason::WEBSOCKET_DISCONNECTED, data);
    }
    connected_ = false;
    authenticated_ = false;
    active_ = false;  // privacy gate closes from the network task immediately
    transport_fault_ = true;
    ++epoch_;
    return;
  } else if (event == WEBSOCKET_EVENT_DATA) {
    // Handle bounded text controls and binary playback, including SDK chunking.
    if (data == nullptr) { record_fault_(FaultReason::MALFORMED_MESSAGE); request_transport_reset_(); return; }
    if (data->op_code == 0x9 || data->op_code == 0xA) return;
    const bool binary = data->op_code == 0x2;
    const int limit = binary ? MAX_PLAYBACK_BYTES : sizeof(control.text) - 1;
    if ((!binary && data->op_code != 0x1) || data->payload_len > limit ||
        data->payload_len < 0 || data->data_len < 0 || data->payload_offset < 0 ||
        data->payload_offset > data->payload_len || data->data_len > data->payload_len - data->payload_offset ||
        (data->data_len > 0 && data->data_ptr == nullptr)) {
      record_fault_(FaultReason::MALFORMED_MESSAGE);
      request_transport_reset_(); return;
    }
    if (data->payload_offset == 0) incoming_.clear();
    if (static_cast<size_t>(data->payload_offset) != incoming_.size()) {
      record_fault_(FaultReason::MALFORMED_MESSAGE); request_transport_reset_(); return;
    }
    if (data->data_len > 0) incoming_.append(data->data_ptr, data->data_len);
    if (incoming_.size() != static_cast<size_t>(data->payload_len)) return;
    if (!data->fin) { record_fault_(FaultReason::MALFORMED_MESSAGE); request_transport_reset_(); return; }
    if (binary) {
      if (!authenticated_) return;
      PlaybackPacket packet{};
      const auto *wire = reinterpret_cast<const uint8_t *>(incoming_.data());
      if (!decode_playback_header(wire, incoming_.size(), packet.header)) {
        record_fault_(FaultReason::MALFORMED_MESSAGE); request_transport_reset_(); return;
      }
      packet.epoch = epoch_;
      std::memcpy(packet.pcm, wire + HEADER_BYTES, incoming_.size() - HEADER_BYTES);
      if (xQueueSend(playback_queue_, &packet, 0) != pdTRUE) playback_overflow_ = true;
      return;
    }
    control.kind = 2;
    control.epoch = epoch_;
    std::memcpy(control.text, incoming_.data(), incoming_.size());
  } else return;
  if (xQueueSend(control_queue_, &control, 0) != pdTRUE) {
    record_fault_(FaultReason::CONTROL_QUEUE_FULL); request_transport_reset_();
  }
}

bool JarvisStream::send_json_(const std::string &text, bool capture_start, bool refresh_diagnostics) {
  if (!connected_ || shutting_down_) return false;
  Outgoing outgoing{};
  if (text.size() >= sizeof(outgoing.text)) {
    record_fault_(FaultReason::OUTGOING_MESSAGE_TOO_LARGE); request_transport_reset_(); return false;
  }
  outgoing.epoch = epoch_;
  outgoing.capture_generation = capture_start ? generation_.load() : 0;
  outgoing.refresh_diagnostics = refresh_diagnostics;
  outgoing.length = text.size();
  std::memcpy(outgoing.text, text.data(), text.size());
  if (xQueueSend(outgoing_queue_, &outgoing, 0) != pdTRUE) {
    record_fault_(FaultReason::OUTGOING_QUEUE_FULL); request_transport_reset_(); return false;
  }
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
  if (muted) {
    active_ = false;
    disarm_wake_capture_("muted");
  }
}

void JarvisStream::record_fault_(FaultReason reason, const esp_websocket_event_data_t *data) {
  record_fault_at_(reason, connection_generation_.load(), generation_.load(), data);
}

void JarvisStream::record_fault_at_(FaultReason reason, uint32_t connection_generation, uint32_t capture_generation,
                                  const esp_websocket_event_data_t *data) {
  FaultDetails details{};
  if (data != nullptr) {
    // Copy only numeric diagnostics supplied by the pinned client's event.
    // In particular, do not read errno after a send or retain event payloads.
    details.has_error = reason == FaultReason::WEBSOCKET_ERROR || reason == FaultReason::WEBSOCKET_DISCONNECTED;
    if (details.has_error) {
      const auto &error = data->error_handle;
      details.ws_error = error.error_type;
      details.http_status = error.esp_ws_handshake_status_code;
      details.has_transport_error = error.error_type == WEBSOCKET_ERROR_TYPE_TCP_TRANSPORT;
      if (details.has_transport_error) {
        details.esp_tls_error = error.esp_tls_last_esp_err;
        details.esp_tls_stack_error = error.esp_tls_stack_err;
        details.esp_tls_flags = error.esp_tls_cert_verify_flags;
        details.socket_errno = error.esp_transport_sock_errno;
      }
    }
    details.ws_close_code = data->close_status_code;
  }
  const auto record = fault_history_.record(reason, now_ms_(), connection_generation, capture_generation, details);
  ESP_LOGW(TAG, "Transport fault #%lu: %s (connection=%lu capture=%lu)",
           static_cast<unsigned long>(record.sequence), FAULT_NAMES[static_cast<size_t>(reason)],
           static_cast<unsigned long>(record.connection_generation), static_cast<unsigned long>(record.capture_generation));
}

void JarvisStream::diagnostics_json_(JsonObject out) {
  const auto snapshot = fault_history_.snapshot();
  write_fault_diagnostics(out, snapshot, boot_id_, now_ms_(), connection_generation_.load(), generation_.load());
  write_pcm_send_diagnostics(out, pcm_send_history_.snapshot());
}

void JarvisStream::send_state_() {
  send_json_(json::build_json([this](JsonObject root) {
    root["type"] = "status";
    root["muted"] = muted_.load();
    root["capturing"] = active_.load();
    root["raw_ready"] = hardware_ready_;
    root["jack_plugged"] = jack_plugged_;
    root["playback_protocol"] = 1;
    root["wake_capture_protocol"] = 1;
    ack_cache_state_(root);
    root["xmos_version"] = xmos_version_;
    diagnostics_json_(root["diagnostics"].to<JsonObject>());
  }));
}

void JarvisStream::command_(const char *text, uint32_t epoch) {
  bool visited = false;
  json::parse_json(std::string(text), [this, &visited, epoch](JsonObject root) {
    visited = true;
    if (!connected_ || epoch != epoch_) return false;
    const std::string type = root["type"] | "";
    if (type == "hello_ok") {
      authenticated_ = true;
      if (connected_ && epoch == epoch_) send_state_();
      else authenticated_ = false;
      return true;
    }
    if (!authenticated_) return false;
    if (type == "clock_sync") {
      const uint32_t stamp = now_ms_();
      send_json_(json::build_json([&](JsonObject out) {
        out["type"] = "clock_sync"; out["id"] = root["id"];
        out["server_time_ms"] = root["server_time_ms"]; out["device_time_ms"] = stamp;
      }));
    } else if (type == "ack_cache_start" || type == "ack_cache_commit" || type == "ack_cache_abort") {
      ack_cache_command_(type, root);
    } else if (type == "play_start" || type == "play_stop" || type == "play_drain") {
      playback_command_(type, root);
    } else if (type == "stop_capture" || type == "start_capture" || type == "arm_wake_capture") {
      capture_command_(type, root, epoch);
    }
    return true;
  });
  if (!visited) record_fault_(FaultReason::MALFORMED_MESSAGE);
}

void JarvisStream::capture_command_(const std::string &type, JsonObject root, uint32_t epoch) {
  const std::string id = root["capture_id"] | "";
  if (type == "stop_capture") {
    if (id.empty()) return;
    if (id == wake_capture_arm_.id) disarm_wake_capture_("requested");
    else if (id == capture_id_) stop_capture_("requested");
    return;
  }

  const bool arm = type == "arm_wake_capture";
  const uint32_t duration = root["duration_ms"] | uint32_t(0);
  const uint32_t wait = root["arm_timeout_ms"] | uint32_t(0);
  const char *error = nullptr;
  if (id.empty() || id.size() > 128 || !duration || duration > WakeCaptureArm::MAX_DURATION_MS ||
      (arm && (!wait || wait > WakeCaptureArm::MAX_WAIT_MS))) error = "invalid_capture";
  else if (!capture_id_.empty()) error = "already_capturing";
  else if (wake_capture_arm_.pending()) error = "already_armed";
  else if (muted_) error = "muted";
  else if (!(hardware_ready_ = verify_hardware_())) error = "raw_stage_unverified";
  else if (!mic_->is_running()) error = "microphone_not_running";
  if (error) {
    send_json_(json::build_json([&](JsonObject out) {
      out["type"] = "error"; out["capture_id"] = id; out["reason"] = error; out["message"] = error;
    }));
    return;
  }

  const uint32_t now = now_ms_();
  if (arm) {
    if (!connected_ || !authenticated_ || epoch != epoch_) return;
    wake_capture_arm_ = {id, duration, now + wait, epoch};
    if (!send_json_(json::build_json([&](JsonObject out) {
      out["type"] = "wake_capture_armed"; out["capture_id"] = id;
    }))) wake_capture_arm_ = {};
  } else if (begin_capture_(id, duration, now, false, epoch)) {
    send_capture_started_(now);
  }
}

bool JarvisStream::begin_capture_(const std::string &id, uint32_t duration, uint32_t started_ms, bool from_wake,
                                  uint32_t expected_epoch) {
  if (!capture_id_.empty() || muted_ || !authenticated_ || !connected_ || expected_epoch != epoch_) return false;
  xQueueReset(audio_queue_);
  capture_id_ = id;
  dropped_ = 0;
  portENTER_CRITICAL(&capture_lock_);
  if (++generation_ == 0) ++generation_;  // Zero means no start acknowledgement in the worker.
  sample_counter_ = 0; sequence_ = 0; deadline_ = started_ms + duration;
  capture_from_wake_ = from_wake;
  capture_wake_ms_ = started_ms;
  portEXIT_CRITICAL(&capture_lock_);
  acknowledged_generation_ = 0;
  pcm_send_history_.start(connection_generation_.load(), generation_.load());
  // The callback can enqueue immediately. The worker sends no PCM until the
  // capture_started acknowledgement is actually transmitted.
  active_ = true;
  if (!authenticated_ || !connected_ || expected_epoch != epoch_) {
    stop_capture_("disconnected");
    return false;
  }
  return true;
}

bool JarvisStream::send_capture_started_(uint32_t started_ms) {
  if (send_json_(json::build_json([&](JsonObject out) {
    out["type"] = "capture_started"; out["capture_id"] = capture_id_;
    out["connection_generation"] = connection_generation_.load();
    out["capture_generation"] = generation_.load();
    out["first_sample"] = 0; out["device_time_ms"] = started_ms;
    out["timing"] = "estimated_callback_minus_block_duration";
  }), true)) return true;
  stop_capture_("transport_error");
  return false;
}

void JarvisStream::ack_cache_state_(JsonObject out) {
  out["ack_cache_protocol"] = 1;
  if (ack_cache_.frames()) out["ack_cache_id"] = ack_cache_.id();
}

void JarvisStream::ack_cache_error_(uint32_t generation, const char *reason) {
  send_json_(json::build_json([&](JsonObject out) {
    out["type"] = "ack_cache_error"; out["generation"] = generation; out["reason"] = reason;
  }));
}

void JarvisStream::abort_ack_cache_() {
  if (!ack_cache_.generation()) return;
  ack_cache_.abort();
  xQueueReset(playback_queue_);
  playback_overflow_ = false;
}

void JarvisStream::ack_cache_command_(const std::string &type, JsonObject root) {
  const uint32_t generation = root["generation"] | uint32_t(0);
  if (type == "ack_cache_start") {
    const uint32_t frames = root["samples"] | uint32_t(0);
    const std::string id = root["cache_id"] | "";
    if (!AckCache::valid_manifest(generation, id, frames)) {
      ack_cache_error_(generation, "invalid_manifest");
      return;
    }
    if (!playback_id_.empty() || ack_cache_.generation()) {
      ack_cache_error_(generation, "busy");
      return;
    }
    AckCache::Pcm pcm(static_cast<uint8_t *>(heap_caps_malloc(frames * PLAYBACK_FRAME_BYTES,
                                                             MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)), std::free);
    if (!pcm) {
      ack_cache_error_(generation, "no_memory");
      return;
    }
    xQueueReset(playback_queue_);
    playback_overflow_ = false;
    ack_cache_.begin(generation, id, frames, std::move(pcm));
    ack_cache_started_ = ack_cache_activity_ = now_ms_();
    if (!send_json_(json::build_json([&](JsonObject out) {
      out["type"] = "ack_cache_ready"; out["generation"] = generation;
    }))) abort_ack_cache_();
    return;
  }
  if (!generation || generation != ack_cache_.generation()) {
    ack_cache_error_(generation, "unknown_generation");
    return;
  }
  if (type == "ack_cache_abort") {
    abort_ack_cache_();
    send_json_(json::build_json([&](JsonObject out) {
      out["type"] = "ack_cache_aborted"; out["generation"] = generation;
    }));
    return;
  }
  if (playback_overflow_.exchange(false) || !ack_cache_.commit()) {
    abort_ack_cache_();
    ack_cache_error_(generation, "incomplete_upload");
    return;
  }
  send_json_(json::build_json([&](JsonObject out) {
    out["type"] = "ack_cache_committed"; out["generation"] = generation;
    out["cache_id"] = ack_cache_.id();
  }));
}

void JarvisStream::ack_cache_loop_(uint32_t now) {
  const uint32_t generation = ack_cache_.generation();
  if (!generation) return;
  if (!authenticated_ || !connected_) {
    abort_ack_cache_();
    return;
  }
  const char *error = nullptr;
  if (playback_overflow_.exchange(false)) error = "queue_overflow";
  else if (now - ack_cache_activity_ >= 2000 || now - ack_cache_started_ >= 10000) error = "upload_timeout";
  else {
    for (int i = 0; i < 4 && xQueueReceive(playback_queue_, &playback_packet_, 0) == pdTRUE; ++i) {
      const auto &packet = playback_packet_;
      if (packet.epoch != epoch_ || packet.header.generation != generation) continue;
      if (!ack_cache_.append(packet.header, packet.pcm)) {
        error = "invalid_sequence";
        break;
      }
      ack_cache_activity_ = now;
      if (!send_json_(json::build_json([&](JsonObject out) {
        out["type"] = "ack_cache_progress"; out["generation"] = generation;
        out["samples"] = ack_cache_.received();
      }))) {
        abort_ack_cache_();
        return;
      }
    }
  }
  if (error) {
    abort_ack_cache_();
    ack_cache_error_(generation, error);
  }
}

const char *JarvisStream::speaker_blocked_() const {
  if (!jack_plugged_) return "jack_unplugged";
  if (muted_) return "muted";
  if (!playback_id_.empty() || !speaker_->is_stopped()) return "speaker_busy";
  if (ack_cache_.generation()) return "cache_upload";
  return nullptr;
}

void JarvisStream::playback_command_(const std::string &type, JsonObject root) {
  const std::string id = root["playback_id"] | "";
  const uint32_t generation = root["generation"] | uint32_t(0);
  if (type != "play_start") {
    if (!playback_id_.empty() && id == playback_id_ && generation == playback_generation_) {
      if (type == "play_stop") stop_playback_("requested");
      else if (!local_ack_playback_) playback_drain_ = true;
    }
    return;
  }
  const char *error = id.empty() || id.size() > 128 || generation == 0 ? "invalid_playback" : speaker_blocked_();
  if (error) {
    send_json_(json::build_json([&](JsonObject out) {
      out["type"] = "play_stopped"; out["playback_id"] = id; out["generation"] = generation;
      out["reason"] = error; out["samples"] = 0;
    }));
    return;
  }
  start_playback_(id, generation);
}

void JarvisStream::playback_identity_(JsonObject out) {
  out["playback_id"] = playback_id_; out["generation"] = playback_generation_;
  if (local_ack_playback_) {
    out["source"] = "wake_ack";
    out["ack_cache_id"] = ack_cache_.id();
  }
}

void JarvisStream::start_playback_(const std::string &id, uint32_t generation, bool local_ack) {
  xQueueReset(playback_queue_);
  playback_overflow_ = false;
  playback_id_ = id;
  playback_generation_ = generation;
  local_ack_playback_ = local_ack;
  playback_samples_ = 0;
  playback_sequence_ = 0;
  playback_activity_ = now_ms_();
  reported_samples_ = 0;
  last_play_clock_report_ = playback_activity_;
  playback_clock_lock_.enter();
  rendered_samples_ = rendered_start_ms_ = rendered_end_ms_ = 0;
  playback_clock_lock_.exit();
  playback_loop_request_.start();
  speaker_->start();
  if (!send_json_(json::build_json([&](JsonObject out) {
    out["type"] = "play_started";
    playback_identity_(out);
    if (local_ack_playback_) out["device_time_ms"] = playback_activity_;
  }))) stop_playback_("transport_error");
}

void JarvisStream::stop_playback_(const char *reason, bool drained) {
  if (playback_id_.empty()) return;
  if (!drained) speaker_->stop();
  xQueueReset(playback_queue_);
  playback_loop_request_.stop();
  playback_pending_ = playback_drain_ = playback_finishing_ = false;
  if (authenticated_ && connected_ && !shutting_down_) {
    playback_clock_lock_.enter();
    const uint32_t rendered = rendered_samples_, start_ms = rendered_start_ms_, end_ms = rendered_end_ms_;
    playback_clock_lock_.exit();
    if (drained && rendered != playback_samples_) {
      drained = false;
      reason = "render_incomplete";
    }
    send_json_(json::build_json([&](JsonObject out) {
      out["type"] = drained ? "play_drained" : "play_stopped";
      playback_identity_(out);
      out["reason"] = reason; out["samples"] = playback_samples_;
      out["rendered_samples"] = rendered; out["last_rendered_ms"] = end_ms;
      if (local_ack_playback_ && rendered) out["first_rendered_ms"] = start_ms;
    }));
  }
  playback_id_.clear();
  local_ack_playback_ = false;
}

void JarvisStream::playback_loop_(uint32_t now) {
  if (playback_id_.empty()) return;
  if (!connected_ || !authenticated_) { stop_playback_("disconnected"); return; }
  if (muted_) { stop_playback_("muted"); return; }
  if (!jack_plugged_) { stop_playback_("jack_unplugged"); return; }
  if (playback_overflow_.exchange(false) && !local_ack_playback_) { stop_playback_("queue_overflow"); return; }
  if (now - playback_activity_ >= 2000) { stop_playback_("playback_timeout"); return; }
  if (!reported_samples_ || now - last_play_clock_report_ >= 100) {
    playback_clock_lock_.enter();
    const uint32_t rendered = rendered_samples_, start_ms = rendered_start_ms_, end_ms = rendered_end_ms_;
    playback_clock_lock_.exit();
    if (rendered != reported_samples_) {
      if (!send_json_(json::build_json([&](JsonObject out) {
        out["type"] = "play_clock";
        playback_identity_(out);
        out["rendered_samples"] = rendered;
        out["last_rendered_ms"] = end_ms; out["timing"] = "i2s_dma_completion";
        if (local_ack_playback_) out["first_rendered_ms"] = start_ms;
      }))) { stop_playback_("transport_error"); return; }
      reported_samples_ = rendered;
      last_play_clock_report_ = now;
    }
  }
  if (playback_finishing_) {
    if (speaker_->is_stopped()) stop_playback_("drained", true);
    return;
  }
  // A normal ESPHome loop tick can exceed one 10 ms wire packet. Drain a
  // bounded batch when the nonblocking speaker buffer has room.
  for (int packet = 0; packet < 4; ++packet) {
    if (!connected_ || !authenticated_) { stop_playback_("disconnected"); return; }
    if (!playback_pending_) {
      const uint8_t *pcm = playback_packet_.pcm;
      if (local_ack_playback_) {
        const uint32_t remaining = ack_cache_.frames() - playback_samples_;
        if (!remaining) {
          speaker_->finish();
          playback_finishing_ = true;
          return;
        }
        const auto frames = static_cast<uint16_t>(std::min<size_t>(MAX_PLAYBACK_FRAMES, remaining));
        playback_packet_.header = {frames, playback_generation_, playback_sequence_, playback_samples_};
        pcm = ack_cache_.pcm() + playback_samples_ * PLAYBACK_FRAME_BYTES;
      } else {
        if (xQueueReceive(playback_queue_, &playback_packet_, 0) != pdTRUE) {
          if (playback_drain_) {
            speaker_->finish();
            playback_finishing_ = true;
          }
          return;
        }
        if (playback_packet_.epoch != epoch_ || playback_packet_.header.generation != playback_generation_) continue;
        if (playback_packet_.header.sequence != playback_sequence_ ||
            playback_packet_.header.first_sample != playback_samples_) {
          stop_playback_("invalid_sequence");
          return;
        }
      }
      playback_pending_ = true;
      playback_offset_ = 0;
      // Match the board's 32-bit I2S slots. Left-align each signed PCM16
      // sample without gain, clipping or dependence on the CPU byte order.
      const size_t samples = playback_packet_.header.samples * 2;
      for (size_t i = 0; i < samples; ++i) {
        playback_i2s_[4 * i] = playback_i2s_[4 * i + 1] = 0;
        playback_i2s_[4 * i + 2] = pcm[2 * i];
        playback_i2s_[4 * i + 3] = pcm[2 * i + 1];
      }
    }
    const auto &header = playback_packet_.header;
    const size_t length = header.samples * 8;
    playback_offset_ += speaker_->play(playback_i2s_ + playback_offset_, length - playback_offset_, 0);
    if (playback_offset_ != length) return;
    playback_activity_ = now_ms_();
    playback_pending_ = false;
    ++playback_sequence_;
    playback_samples_ += header.samples;
    if (!send_json_(json::build_json([&](JsonObject out) {
      out["type"] = "play_render";
      playback_identity_(out);
      out["first_sample"] = header.first_sample;
      out["samples"] = header.samples; out["device_time_ms"] = playback_activity_;
      out["timing"] = "speaker_buffer_acceptance";
    }))) { stop_playback_("transport_error"); return; }
  }
}

void JarvisStream::microphone_data_(const std::vector<uint8_t> &data) {
  if (!active_ || muted_ || !authenticated_ || !connected_) return;
  // MicrophoneSource supplies 16-bit, mono, gain=1. Never block its high-priority task on Wi-Fi.
  const size_t samples = data.size() / 2;
  const uint32_t callback_ms = now_ms_();
  size_t offset = 0;
  uint32_t callback_generation;
  portENTER_CRITICAL(&capture_lock_);
  callback_generation = generation_;
  if (capture_from_wake_) offset = wake_capture_sample_offset(samples, callback_ms, capture_wake_ms_);
  portEXIT_CRITICAL(&capture_lock_);
  for (; offset < samples; offset += MAX_SAMPLES) {
    AudioPacket packet{};
    packet.samples = std::min(MAX_SAMPLES, samples - offset);
    uint64_t first_sample;
    uint32_t sequence;
    portENTER_CRITICAL(&capture_lock_);
    if (!active_ || muted_ || callback_generation != generation_ || static_cast<int32_t>(callback_ms - deadline_) >= 0) {
      portEXIT_CRITICAL(&capture_lock_); return;
    }
    packet.generation = generation_;
    first_sample = sample_counter_; sample_counter_ += packet.samples;
    sequence = sequence_++;
    portEXIT_CRITICAL(&capture_lock_);
    const uint32_t stamp = callback_ms - static_cast<uint32_t>((samples - offset) * 1000 / 16000);
    encode_header(packet.wire, packet.samples, sequence, first_sample, stamp);
    std::memcpy(packet.wire + HEADER_BYTES, data.data() + offset * 2, packet.samples * 2);
    packet.enqueued_ms = now_ms_();
    if (xQueueSend(audio_queue_, &packet, 0) != pdTRUE) ++dropped_;
  }
}

void JarvisStream::disarm_wake_capture_(const char *reason) {
  if (!wake_capture_arm_.pending()) return;
  auto arm = wake_capture_arm_.take();
  if (authenticated_ && arm.epoch == epoch_) {
    send_json_(json::build_json([&](JsonObject out) {
      out["type"] = "wake_capture_disarmed"; out["capture_id"] = arm.id; out["reason"] = reason;
    }));
  }
}

void JarvisStream::stop_capture_(const char *reason) {
  active_ = false;
  disarm_wake_capture_(reason);
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
      diagnostics_json_(out["diagnostics"].to<JsonObject>());
    }), false, true);
  }
  capture_id_.clear();
}

void JarvisStream::wake_word(const std::string &word) {
  if (muted_ || !authenticated_ || !connected_ || shutting_down_) return;
  const uint32_t detected_ms = now_ms_();
  bool capture_started = false;
  if (wake_capture_arm_.pending()) {
    if (wake_capture_arm_.expired(detected_ms)) disarm_wake_capture_("arm_timeout");
    else if (wake_capture_arm_.can_fire(word, detected_ms, epoch_.load())) {
      if (!hardware_ready_) disarm_wake_capture_("raw_stage_unverified");
      else if (!mic_->is_running()) disarm_wake_capture_("microphone_not_running");
      else {
        auto arm = wake_capture_arm_.take();  // One shot, including if start loses the connection.
        capture_started = begin_capture_(arm.id, arm.duration_ms, detected_ms, true, arm.epoch);
      }
    }
  }
  const char *reason = word == "Hey Jarvis" ? speaker_blocked_() : "different_wake_word";
  if (!reason && !ack_cache_.frames()) reason = "cache_missing";
  std::string ack_playback_id;
  if (!reason) {
    if (++ack_generation_ == 0) ++ack_generation_;
    ack_playback_id = "wake-ack-" + std::to_string(ack_generation_);
  }
  if (!send_json_(json::build_json([&](JsonObject out) {
    out["type"] = "wake_word"; out["wake_word"] = word; out["device_time_ms"] = detected_ms;
    out["capture_id"] = capture_id_;
    if (reason) out["ack_reason"] = reason;
    else {
      out["ack_playback_id"] = ack_playback_id;
      out["ack_generation"] = ack_generation_;
      out["ack_cache_id"] = ack_cache_.id();
      out["ack_samples"] = ack_cache_.frames();
    }
  }))) {
    if (capture_started) stop_capture_("transport_error");
    return;
  }
  if (capture_started && !send_capture_started_(detected_ms)) return;
  if (!reason) start_playback_(ack_playback_id, ack_generation_, true);
}

void JarvisStream::loop() {
  const uint32_t now = now_ms_();
  const bool network_ready = network::is_connected();
  if (!network_ready && network_ready_.exchange(false)) {
    record_fault_(FaultReason::WIFI_LOST);
    // A brief Wi-Fi flap may leave TCP apparently alive. Force a fresh hello on recovery.
    request_transport_reset_();
  } else if (network_ready) network_ready_ = true;
  if (transport_fault_.exchange(false)) {
    stop_capture_("disconnected");
    stop_playback_("disconnected");
    abort_ack_cache_();
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
      stop_playback_("reconnected");
      abort_ack_cache_();
      send_json_(json::build_json([this](JsonObject out) {
        out["type"] = "hello"; out["protocol"] = 1; out["device_id"] = device_id_;
        out["token"] = token_; out["sample_rate"] = 16000; out["channels"] = 1;
        out["firmware"] = ESPHOME_PROJECT_VERSION; out["firmware_project"] = ESPHOME_PROJECT_NAME;
        out["xmos_version"] = xmos_version_;
        out["muted"] = muted_.load(); out["raw_stage"] = "NONE";
        out["playback_protocol"] = 1; out["jack_plugged"] = jack_plugged_;
        out["wake_capture_protocol"] = 1;
        ack_cache_state_(out);
        diagnostics_json_(out["diagnostics"].to<JsonObject>());
      }));
    } else command_(control.text, control.epoch);
  }
  if (muted_ != previous_mute_) { previous_mute_ = muted_; if (authenticated_) send_state_(); }
  if (jack_plugged_ != previous_jack_) { previous_jack_ = jack_plugged_; if (authenticated_) send_state_(); }
  ack_cache_loop_(now_ms_());
  playback_loop_(now_ms_());
  if (wake_capture_arm_.pending()) {
    if (!authenticated_ || !connected_ || wake_capture_arm_.epoch != epoch_) disarm_wake_capture_("disconnected");
    else if (!hardware_ready_) disarm_wake_capture_("raw_stage_unverified");
    else if (wake_capture_arm_.expired(now_ms_())) disarm_wake_capture_("arm_timeout");
  }
  if (!capture_id_.empty() && (!active_ || muted_ || static_cast<int32_t>(now - deadline_) >= 0)) {
    stop_capture_(muted_ ? "muted" : "duration_complete");
  }
}

void JarvisStream::on_shutdown() {
  // ESPHome shutdown/OTA must not wait for a network task, socket or DNS operation.
  active_ = false; authenticated_ = false; connected_ = false;
  disarm_wake_capture_("shutdown");
  stop_playback_("shutdown");
  abort_ack_cache_();
  network_ready_ = false; shutting_down_ = true;
}
}  // namespace esphome::jarvis_stream
