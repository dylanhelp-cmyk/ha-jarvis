#pragma once
#include <atomic>
#include <string>
#include "esphome/core/component.h"
#include "esphome/components/i2c/i2c.h"
#include "esphome/components/microphone/microphone_source.h"
#include "esp_websocket_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "protocol.h"

namespace esphome::jarvis_stream {
class JarvisStream : public Component, public i2c::I2CDevice {
 public:
  void set_microphone(microphone::MicrophoneSource *mic) { mic_ = mic; }
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
  struct AudioPacket { uint32_t generation; uint16_t samples; uint8_t wire[HEADER_BYTES + MAX_SAMPLES * 2]; };
  struct Control { uint32_t epoch; uint8_t kind; char text[768]; };
  static void websocket_event(void *arg, esp_event_base_t base, int32_t event, void *data);
  void receive_event_(int32_t event, esp_websocket_event_data_t *data);
  void microphone_data_(const std::vector<uint8_t> &data);
  void command_(const char *text);
  bool send_json_(const std::string &text);
  void stop_capture_(const char *reason);
  bool verify_hardware_();
  void send_state_();
  static uint32_t now_ms_();
  microphone::MicrophoneSource *mic_{nullptr};
  esp_websocket_client_handle_t client_{nullptr};
  QueueHandle_t audio_queue_{nullptr}, control_queue_{nullptr};
  std::string server_url_, device_id_, token_, capture_id_, incoming_;
  std::atomic<bool> active_{false}, authenticated_{false}, connected_{false}, muted_{true}, transport_fault_{false};
  std::atomic<uint32_t> epoch_{0}, dropped_{0};
  portMUX_TYPE capture_lock_ = portMUX_INITIALIZER_UNLOCKED;
  uint64_t sample_counter_{0};
  uint32_t sequence_{0}, generation_{0}, deadline_{0};
  uint32_t last_connect_attempt_{0}, last_hardware_check_{0};
  bool hardware_ready_{false}, previous_mute_{true};
  std::string xmos_version_;
};
}  // namespace esphome::jarvis_stream
