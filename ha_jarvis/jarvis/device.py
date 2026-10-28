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
        return parsed
    except (ValueError, KeyError, TypeError):
        return None


class Device:
    def __init__(self):
        self.ws = None
        self.info = {}
        self.capture_id = None
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.frames = []
        self.clock_offset_ms = None
        self.clock_rtt_ms = None
        self.sync_pending = {}
        self.errors = []
        self.next_sample = None
        self.last_seq = None
        self.stop_info = None
        self.last_frame_at = None
        self.capture_session = {}

    def snapshot(self):
        return {"connected": self.ws is not None and not self.ws.closed, **self.info,
                "clock_rtt_ms": self.clock_rtt_ms, "capturing": self.capture_id is not None}

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
        if kind == "clock_sync":
            sent = self.sync_pending.pop(str(message.get("id")), None)
            if sent is not None:
                received = time.monotonic() * 1000
                rtt = received - sent
                if self.clock_rtt_ms is None or rtt < self.clock_rtt_ms:
                    self.clock_rtt_ms = rtt
                    self.clock_offset_ms = (sent + received) / 2 - float(message["device_time_ms"])
        elif kind == "capture_started" and message.get("capture_id") == self.capture_id:
            for key in ("connection_generation", "capture_generation"):
                number = message.get(key)
                if type(number) is int and 0 <= number <= 0xFFFFFFFF:
                    self.capture_session[key] = number
            self.started.set()
        elif kind in ("mute", "status", "muted"):
            for key in ("muted", "capturing", "raw_ready", "xmos_version"):
                if key in message: self.info[key] = message[key]
            if self.capture_id and self.info.get("muted"):
                self.errors.append("Physical mute interrupted capture")
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
        if sum(len(f.samples) for f in self.frames) + len(frame.samples) > RATE * 120:
            raise ValueError("Capture exceeded the 120-second memory limit")
        self.frames.append(frame)
        self.last_frame_at = time.monotonic()

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

    async def begin(self, capture_id, duration_ms):
        if not self.snapshot()["connected"] or self.info.get("muted"):
            raise ValueError("Voice PE must be connected and physically unmuted")
        if self.info.get("raw_ready") is not True:
            raise ValueError("Voice PE has not confirmed the raw microphone stage and compatible XMOS firmware")
        self.frames.clear()
        self.errors.clear()
        self.next_sample = self.last_seq = None
        self.stop_info = self.last_frame_at = None
        self.capture_id = capture_id
        diagnostics = self.info.get("diagnostics", {})
        self.capture_session = {key: diagnostics[key] for key in ("boot_id", "connection_generation") if key in diagnostics}
        self.started.clear()
        self.stopped.clear()
        await self.sync()
        await self.ws.send_json({"type": "start_capture", "capture_id": capture_id, "duration_ms": duration_ms})
        await asyncio.wait_for(self.started.wait(), timeout=5)

    async def end(self):
        if self.capture_id is not None and self.ws is not None and not self.ws.closed:
            await self.ws.send_json({"type": "stop_capture", "capture_id": self.capture_id})
            if self.started.is_set() and not self.stopped.is_set():
                try:
                    await asyncio.wait_for(self.stopped.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self.errors.append("Device did not confirm capture completion and drop count")
        self.capture_id = None

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
