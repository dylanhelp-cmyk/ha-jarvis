#pragma once
#include <atomic>
#include <string>
#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/components/i2c/i2c.h"
#include "esphome/components/microphone/microphone_source.h"
#include "esphome/components/speaker/speaker.h"
#include "esphome/components/json/json_util.h"
#include "esp_websocket_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "network_lifecycle.h"
#include "fault_history.h"
#include "pcm_send_history.h"
#include "protocol.h"
#include "ack_cache.h"
#include "wake_capture.h"

namespace esphome::jarvis_stream {
struct FaultLock {
  void enter() { portENTER_CRITICAL(&mux); }
  void exit() { portEXIT_CRITICAL(&mux); }
  portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
};
class JarvisStream : public Component, public i2c::I2CDevice {
 public:
  void set_capture_gain(uint8_t gain) { capture_gain_ = gain; }
  void set_microphone(microphone::MicrophoneSource *mic) { mic_ = mic; }
  void set_speaker(speaker::Speaker *speaker) { speaker_ = speaker; }
  void set_jack_plugged(bool plugged) { jack_plugged_ = plugged; }
  void set_server_url(const std::string &value) { server_url_ = value; }
  void set_device_id(const std::string &value) { device_id_ = value; }
  void set_token(const std::string &value) { token_ = value; }
  void setup() override;
  void loop() override;
  void on_shutdown() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }
  void set_muted(bool muted);
  bool is_capturing() const { return active_.load(); }
  bool is_ready() const { return authenticated_.load() && hardware_ready_; }
  void wake_word(const std::string &word);

 protected:
  struct AudioPacket { uint32_t generation; uint32_t enqueued_ms; uint16_t samples; uint8_t wire[HEADER_BYTES + MAX_SAMPLES * 2]; };
  struct Outgoing { uint32_t epoch; uint32_t capture_generation; uint16_t length; bool refresh_diagnostics; char text[3072]; };
  struct Control { uint32_t epoch; uint8_t kind; char text[768]; };
  struct PlaybackPacket {
    uint32_t epoch;
    PlaybackHeader header;
    uint8_t pcm[MAX_PLAYBACK_FRAMES * PLAYBACK_FRAME_BYTES];
  };
  static void network_worker(void *arg);
  void network_worker_();
  bool initialize_client_();
  void request_transport_reset_();
  static void websocket_event(void *arg, esp_event_base_t base, int32_t event, void *data);
  void receive_event_(int32_t event, esp_websocket_event_data_t *data);
  void microphone_data_(const std::vector<uint8_t> &data);
  void command_(const char *text, uint32_t epoch);
  bool send_json_(const std::string &text, bool capture_start = false, bool refresh_diagnostics = false);
  void capture_command_(const std::string &type, JsonObject root, uint32_t epoch);
  bool begin_capture_(const std::string &id, uint32_t duration, uint32_t started_ms, bool from_wake,
                      uint32_t expected_epoch);
  bool send_capture_started_(uint32_t started_ms);
  void stop_capture_(const char *reason);
  void disarm_wake_capture_(const char *reason);
  void ack_cache_command_(const std::string &type, JsonObject root);
  void ack_cache_loop_(uint32_t now);
  void abort_ack_cache_();
  void ack_cache_error_(uint32_t generation, const char *reason);
  void ack_cache_state_(JsonObject out);
  const char *speaker_blocked_() const;
  void playback_command_(const std::string &type, JsonObject root);
  void start_playback_(const std::string &id, uint32_t generation, bool local_ack = false);
  void playback_identity_(JsonObject out);
  void playback_loop_(uint32_t now);
  void stop_playback_(const char *reason, bool drained = false);
  bool verify_hardware_();
  void send_state_();
  void record_fault_(FaultReason reason, const esp_websocket_event_data_t *data = nullptr);
  void record_fault_at_(FaultReason reason, uint32_t connection_generation, uint32_t capture_generation,
                        const esp_websocket_event_data_t *data = nullptr);
  void diagnostics_json_(JsonObject out);
  static uint32_t now_ms_();
  uint8_t capture_gain_{1};
  microphone::MicrophoneSource *mic_{nullptr};
  speaker::Speaker *speaker_{nullptr};
  esp_websocket_client_handle_t client_{nullptr};
  QueueHandle_t audio_queue_{nullptr}, control_queue_{nullptr}, outgoing_queue_{nullptr};
  QueueHandle_t playback_queue_{nullptr};
  TaskHandle_t network_worker_handle_{nullptr};
  std::string server_url_, device_id_, token_, capture_id_, incoming_;
  std::atomic<bool> active_{false}, authenticated_{false}, connected_{false}, muted_{true}, transport_fault_{false};
  std::atomic<bool> network_ready_{false}, worker_reset_{false}, shutting_down_{false};
  std::atomic<bool> intentional_stop_{false};
  std::atomic<bool> playback_overflow_{false};
  HighFrequencyLoopRequester playback_loop_request_;
  AckCache ack_cache_;
  WakeCaptureArm wake_capture_arm_;
  uint32_t ack_cache_started_{0}, ack_cache_activity_{0}, ack_generation_{0};
  std::string playback_id_;
  uint32_t playback_generation_{0}, playback_sequence_{0}, playback_activity_{0};
  uint32_t last_play_clock_report_{0};
  uint64_t playback_samples_{0};
  FaultLock playback_clock_lock_;
  uint32_t rendered_samples_{0}, rendered_start_ms_{0}, rendered_end_ms_{0}, reported_samples_{0};
  PlaybackPacket playback_packet_{};
  uint8_t playback_i2s_[MAX_PLAYBACK_FRAMES * 8]{};
  size_t playback_offset_{0};
  bool playback_pending_{false}, playback_drain_{false}, playback_finishing_{false};
  bool local_ack_playback_{false};
  bool jack_plugged_{false}, previous_jack_{false};
  std::atomic<uint32_t> epoch_{0}, dropped_{0}, generation_{0}, acknowledged_generation_{0};
  std::atomic<uint32_t> connection_generation_{0};
  FaultHistory<FaultLock> fault_history_;
  PcmSendHistory<FaultLock> pcm_send_history_;
  char boot_id_[17]{};
  portMUX_TYPE capture_lock_ = portMUX_INITIALIZER_UNLOCKED;
  uint64_t sample_counter_{0};
  uint32_t sequence_{0}, deadline_{0};
  uint32_t capture_wake_ms_{0};
  bool capture_from_wake_{false};
  uint32_t last_hardware_check_{0};
  bool hardware_ready_{false}, previous_mute_{true};
  std::string xmos_version_;
};
}  // namespace esphome::jarvis_stream
