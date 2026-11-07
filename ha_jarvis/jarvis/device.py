"""Paired device stream and timestamp accounting."""
from __future__ import annotations

import asyncio
import re
import time

import numpy as np

from .audio import capture_health, decode_frame, RATE


def parse_diagnostics(value):
    """Only retain the bounded, credential-free firmware telemetry schema."""
    if not isinstance(value, dict) or value.get("schema") != 1:
        return None
    boot = value.get("boot_id")
    if not isinstance(boot, str) or not re.fullmatch(r"[0-9a-f]{16}", boot):
        return None
    def uint(number):
        if type(number) is not int or not 0 <= number <= 0xFFFFFFFF:
            raise ValueError("Invalid telemetry counter")
        return number
    def fault(record):
        if record is None:
            return None
        if not isinstance(record, dict) or not isinstance(record.get("reason"), str) or not re.fullmatch(r"[a-z_]{1,64}", record["reason"]):
            raise ValueError("Invalid telemetry reason")
        parsed = {"reason": record["reason"]}
        for key in ("sequence", "uptime_ms", "connection_generation", "capture_generation"):
            parsed[key] = uint(record[key])
        for key in ("ws_error", "esp_tls_error", "esp_tls_stack_error", "esp_tls_flags", "errno", "http_status", "ws_close_code"):
            if key in record:
                number = record[key]
                if type(number) is not int or not -(2 ** 31) <= number <= 0xFFFFFFFF:
                    raise ValueError("Invalid SDK error value")
                parsed[key] = number
        return parsed
    def pcm_send(summary):
        if summary is None:
            return None
        try:
            if not isinstance(summary, dict):
                return None
            parsed = {key: uint(summary[key]) for key in (
                "connection_generation", "capture_generation", "send_count", "failed_send_count",
                "max_success_duration_ms", "max_failed_duration_ms", "max_queue_age_ms")}
            failure = summary.get("last_failed_send")
            parsed["last_failed_send"] = None
            if failure is not None:
                returned = failure["returned_bytes"]
                if type(returned) is not int or not -(2 ** 31) <= returned < 2 ** 31:
                    return None
                parsed["last_failed_send"] = {"returned_bytes": returned,
                    **{key: uint(failure[key]) for key in ("expected_bytes", "duration_ms", "uptime_ms")}}
            return parsed
        except (ValueError, KeyError, TypeError):
            # Optional telemetry cannot invalidate otherwise useful fault history.
            return None
    try:
        parsed = {"schema": 1, "boot_id": boot}
        for key in ("uptime_ms", "connection_generation", "capture_generation", "fault_sequence"):
            parsed[key] = uint(value[key])
        counts = value.get("fault_counts", {})
        if not isinstance(counts, dict) or len(counts) > 16:
            return None
        parsed["fault_counts"] = {key: uint(count) for key, count in counts.items()
                                  if isinstance(key, str) and re.fullmatch(r"[a-z_]{1,64}", key)}
        parsed["root_fault"] = fault(value.get("root_fault"))
        parsed["last_fault"] = fault(value.get("last_fault"))
        parsed["transport_fault"] = fault(value.get("transport_fault"))
        parsed["pcm_send"] = pcm_send(value.get("pcm_send"))
        return parsed
    except (ValueError, KeyError, TypeError):
        return None


class Device:
    def __init__(self):
        self.ws = None
        self.info = {}
        self.capture_id = None
        self.started = asyncio.Event()
        self.armed = asyncio.Event()
        self.awaiting_wake = False
        self.arm_stop_reason = None
        self.stopped = asyncio.Event()
        self.frames = []
        self._capture_samples = 0
        self.clock_offset_ms = None
        self.clock_rtt_ms = None
        self.sync_pending = {}
        self.errors = []
        self.next_sample = None
        self.last_seq = None
        self.stop_info = None
        self.last_frame_at = None
        self.capture_session = {}
        self.pcm_consumer = None
        self.playback = None
        self.wake_cache_upload = None
        self.wake_count = 0
        self.last_wake_at = None
        self.wake_event = asyncio.Event()
        self.wake_details = {}
        self.wake_playback_terminal = {}
        self.wake_render_progress = None
        self.wake_render_latest = None
        self.wake_render_changed = asyncio.Event()
        self.wake_render_terminal_at = None
        self.wake_terminal_event = asyncio.Event()

    def snapshot(self):
        return {"connected": self.ws is not None and not self.ws.closed, **self.info,
                "clock_rtt_ms": self.clock_rtt_ms,
                "capturing": self.capture_id is not None and (not self.awaiting_wake or self.started.is_set()),
                "wake_capture_armed": self.awaiting_wake and self.armed.is_set() and not self.started.is_set(),
                "wake_count": self.wake_count,
                "last_wake_age_ms": (round((time.monotonic() - self.last_wake_at) * 1000)
                                     if self.last_wake_at is not None else None)}

    async def sync(self):
        self.clock_offset_ms = self.clock_rtt_ms = None
        self.sync_pending.clear()
        for i in range(4):
            now = time.monotonic() * 1000
            self.sync_pending[str(i)] = now
            await self.ws.send_json({"type": "clock_sync", "id": str(i), "server_time_ms": now})
            await asyncio.sleep(.075)
        await asyncio.sleep(.2)
        if self.clock_offset_ms is None:
            raise ValueError("Device clock sync failed; acoustic timing is unavailable")

    def receive_json(self, message):
        if "diagnostics" in message:
            diagnostics = parse_diagnostics(message["diagnostics"])
            if diagnostics is not None:
                self.info["diagnostics"] = diagnostics
        kind = message.get("type")
        if kind in ("play_started", "play_render", "play_clock", "play_stopped", "play_drained"):
            if self.playback is not None:
                self.playback.receive(message)
            if (message.get("source") == "wake_ack" and self.wake_details.get("ack_playback_id")
                    and message.get("playback_id") == self.wake_details.get("ack_playback_id")
                    and message.get("generation") == self.wake_details.get("ack_generation")):
                if kind == "play_clock" and not self.wake_terminal_event.is_set():
                    samples, first, last = (message.get(key) for key in
                                            ("rendered_samples", "first_rendered_ms", "last_rendered_ms"))
                    expected = self.wake_details.get("ack_samples")
                    cache_id = self.wake_details.get("ack_cache_id")
                    # Use at least 80 ms of confirmed 48 kHz output so the
                    # prefix can cover microphone audio after speaker onset.
                    if (cache_id and message.get("ack_cache_id") == cache_id
                            and all(type(value) is int for value in (samples, first, last, expected))
                            and 3840 <= samples < expected and 0 <= first < last <= 0xFFFFFFFF
                            and abs(last - first - samples / 48) <= 15):
                        previous = self.wake_render_latest
                        if previous is None or (samples > previous[0] and first == previous[1]):
                            report = (samples, first, time.monotonic())
                            if self.wake_render_progress is None:
                                self.wake_render_progress = report
                            self.wake_render_latest = report
                            self.wake_render_changed.set()
                elif kind in ("play_stopped", "play_drained"):
                    self.wake_playback_terminal = {key: message[key] for key in
                        ("rendered_samples", "first_rendered_ms", "last_rendered_ms", "reason") if key in message}
                    self.wake_playback_terminal["drained"] = kind == "play_drained"
                    self.wake_render_terminal_at = time.monotonic()
                    self.wake_terminal_event.set()
                    self.wake_render_changed.set()
        elif kind in ("ack_cache_ready", "ack_cache_progress", "ack_cache_committed", "ack_cache_error"):
            if self.wake_cache_upload is not None:
                self.wake_cache_upload.receive(message)
        elif kind == "clock_sync":
            sent = self.sync_pending.pop(str(message.get("id")), None)
            if sent is not None:
                received = time.monotonic() * 1000
                rtt = received - sent
                if self.clock_rtt_ms is None or rtt < self.clock_rtt_ms:
                    self.clock_rtt_ms = rtt
                    self.clock_offset_ms = (sent + received) / 2 - float(message["device_time_ms"])
        elif kind == "wake_capture_armed" and self.awaiting_wake and message.get("capture_id") == self.capture_id:
            self.armed.set()
        elif kind == "wake_capture_disarmed" and self.awaiting_wake and message.get("capture_id") == self.capture_id:
            reason = message.get("reason")
            self.arm_stop_reason = reason if reason in ("requested", "muted", "arm_timeout",
                "raw_stage_unverified", "microphone_not_running") else "device_disarmed"
            self.stopped.set()
        elif kind == "capture_started" and self.capture_id is not None and message.get("capture_id") == self.capture_id:
            for key in ("connection_generation", "capture_generation"):
                number = message.get(key)
                if type(number) is int and 0 <= number <= 0xFFFFFFFF:
                    self.capture_session[key] = number
            self.started.set()
        elif kind in ("mute", "status", "muted"):
            for key in ("muted", "capturing", "raw_ready", "xmos_version", "jack_plugged", "playback_protocol", "ack_cache_protocol", "ack_cache_id", "wake_capture_protocol", "capture_gain"):
                if key in message: self.info[key] = message[key]
            if self.capture_id and self.info.get("muted"):
                self.errors.append("Physical mute interrupted capture")
        elif kind == "wake_word" and self.info.get("muted") is False:
            word = message.get("wake_word")
            if isinstance(word, str) and word.strip().casefold().replace("_", " ") == "hey jarvis":
                self.wake_count += 1
                self.last_wake_at = time.monotonic()
                if (not self.wake_event.is_set() and
                        (not self.awaiting_wake or message.get("capture_id") == self.capture_id)):
                    self.wake_details = {key: message[key] for key in
                        ("device_time_ms", "capture_id", "ack_playback_id", "ack_generation", "ack_cache_id",
                         "ack_samples", "ack_reason") if key in message}
                    self.wake_playback_terminal = {}
                    self.wake_render_progress = None
                    self.wake_render_latest = None
                    self.wake_render_changed.clear()
                    self.wake_render_terminal_at = None
                    self.wake_event.set()
        elif kind == "capture_stopped" and message.get("capture_id") == self.capture_id:
            self.stop_info = {key: message[key] for key in ("reason", "samples_seen", "dropped_frames") if key in message}
            self.stopped.set()
            if message.get("dropped_frames", 0):
                self.errors.append("Firmware dropped microphone frames")
            if message.get("reason") not in ("requested", "server", "stop_capture", "normal"):
                self.errors.append("Device stopped capture: " + str(message.get("reason", "unknown")))
        elif kind == "error":
            self.errors.append(str(message.get("message", "Device capture error")))

    def receive_pcm(self, data):
        if self.capture_id is None:
            return
        if self.awaiting_wake and not self.started.is_set():
            self.errors.append("Microphone PCM arrived before the armed capture started")
            return
        frame = decode_frame(data)
        if self.next_sample is None and (frame.first_sample != 0 or frame.sequence != 0):
            self.errors.append("Capture beginning was lost")
        if self.next_sample is not None and frame.first_sample != self.next_sample:
            self.errors.append("Capture sample gap or reordering")
        if self.last_seq is not None and frame.sequence != (self.last_seq + 1) % (2 ** 32):
            self.errors.append("Capture sequence gap or reordering")
        self.next_sample = frame.first_sample + len(frame.samples)
        self.last_seq = frame.sequence
        # Never maintain an unbounded microphone backlog.
        total_samples = self._capture_samples + len(frame.samples)
        if total_samples > RATE * 120:
            raise ValueError("Capture exceeded the 120-second memory limit")
        self.frames.append(frame)
        self._capture_samples = total_samples
        self.last_frame_at = time.monotonic()
        if self.pcm_consumer is not None:
            self.pcm_consumer(frame, self.clock_offset_ms)

    def capture_diagnostics(self):
        """Partial recordings remain diagnostically useful, never valid measurements."""
        pcm = np.concatenate([frame.samples for frame in self.frames]) if self.frames else np.empty(0, dtype="<i2")
        health = capture_health(pcm)
        health.update(frames=len(self.frames), errors=list(dict.fromkeys(self.errors)),
                      completion_confirmed=self.stopped.is_set(), stop=self.stop_info,
                      clock_rtt_ms=self.clock_rtt_ms)
        if self.last_frame_at is not None:
            health["last_frame_age_ms"] = round((time.monotonic() - self.last_frame_at) * 1000, 1)
        if len(self.frames) > 1:
            span_ms = (self.frames[-1].capture_ms - self.frames[0].capture_ms) % (2 ** 32)
            sample_span_ms = (self.frames[-1].first_sample - self.frames[0].first_sample) * 1000 / RATE
            health.update(timestamp_span_ms=span_ms, sample_clock_span_ms=round(sample_span_ms, 1),
                          span_difference_ms=round(span_ms - sample_span_ms, 1))
        return health

    async def begin(self, capture_id, duration_ms, *, clock_synced=False, arm_timeout_ms=None):
        if not self.snapshot()["connected"] or self.info.get("muted"):
            raise ValueError("Voice PE must be connected and physically unmuted")
        if self.info.get("raw_ready") is not True:
            raise ValueError("Voice PE has not confirmed the raw microphone stage and compatible XMOS firmware")
        self.frames.clear()
        self._capture_samples = 0
        self.errors.clear()
        self.next_sample = self.last_seq = None
        self.stop_info = self.last_frame_at = None
        self.capture_id = capture_id
        diagnostics = self.info.get("diagnostics", {})
        self.capture_session = {key: diagnostics[key] for key in ("boot_id", "connection_generation") if key in diagnostics}
        self.started.clear()
        self.armed.clear()
        self.arm_stop_reason = None
        self.stopped.clear()
        self.awaiting_wake = arm_timeout_ms is not None
        if not clock_synced:
            await self.sync()
        command = {"type": "start_capture", "capture_id": capture_id, "duration_ms": duration_ms}
        if self.awaiting_wake:
            command.update(type="arm_wake_capture", arm_timeout_ms=arm_timeout_ms)
        await self.ws.send_json(command)
        await asyncio.wait_for((self.armed if self.awaiting_wake else self.started).wait(), timeout=5)

    async def end(self):
        if self.capture_id is not None and self.ws is not None and not self.ws.closed:
            await self.ws.send_json({"type": "stop_capture", "capture_id": self.capture_id})
            if (self.started.is_set() or self.armed.is_set()) and not self.stopped.is_set():
                try:
                    await asyncio.wait_for(self.stopped.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self.errors.append("Device did not confirm capture completion and drop count")
        self.capture_id = None
        self.awaiting_wake = False

    def recording(self):
        if not self.frames:
            raise ValueError("No microphone frames received")
        if self.errors:
            raise ValueError("; ".join(dict.fromkeys(self.errors)))
        if self.clock_offset_ms is None or self.clock_rtt_ms > 100:
            raise ValueError("Clock timing uncertainty is too high")
        first = self.frames[0]
        # Device millis rolls over after ~49 days. The sync and first frame must
        # use the same wrap epoch; reject ambiguous captures rather than guess.
        start = (first.capture_ms + self.clock_offset_ms) / 1000
        if abs(start - time.monotonic()) > 125:
            raise ValueError("Device clock wrapped or changed during capture")
        pcm = np.concatenate([f.samples for f in self.frames])
        return pcm, start
