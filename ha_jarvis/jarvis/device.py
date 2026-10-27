"""Paired device stream and timestamp accounting."""
from __future__ import annotations

import asyncio
import time

import numpy as np

from .audio import decode_frame, RATE


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
            self.started.set()
        elif kind in ("mute", "status", "muted"):
            for key in ("muted", "capturing", "raw_ready", "xmos_version"):
                if key in message: self.info[key] = message[key]
            if self.capture_id and self.info.get("muted"):
                self.errors.append("Physical mute interrupted capture")
        elif kind == "capture_stopped" and message.get("capture_id") == self.capture_id:
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

    async def begin(self, capture_id, duration_ms):
        if not self.snapshot()["connected"] or self.info.get("muted"):
            raise ValueError("Voice PE must be connected and physically unmuted")
        if self.info.get("raw_ready") is not True:
            raise ValueError("Voice PE has not confirmed the raw microphone stage and compatible XMOS firmware")
        self.frames.clear()
        self.errors.clear()
        self.next_sample = self.last_seq = None
        self.capture_id = capture_id
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
