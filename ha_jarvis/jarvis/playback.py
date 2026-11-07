"""Bounded PCM sender for the Voice PE jack."""
from __future__ import annotations

import asyncio
from collections import deque
import secrets
import struct
import time

import numpy as np


RATE = 48000
CHANNELS = 2
PACKET_FRAMES = 480
HEADER = struct.Struct("<4sBBHIIQ")
MAX_IN_FLIGHT = 6 * PACKET_FRAMES


def stereo_pcm(samples):
    """Upsample a 16 kHz bench signal to 48 kHz stereo PCM16LE."""
    import av

    mono = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
    mono.sample_rate = RATE // 3
    converter = av.AudioResampler(format="s16", layout="mono", rate=RATE)
    blocks = converter.resample(mono) + converter.resample(None)
    return b"".join(np.repeat(block.to_ndarray().ravel(), 2).astype("<i2").tobytes() for block in blocks)


class WakeCacheUpload:
    """Install one bounded cache over the current authenticated device socket."""

    def __init__(self, device):
        self.device = device
        self.ws = device.ws
        self.generation = secrets.randbelow(0xFFFFFFFF) + 1
        self.replies = asyncio.Queue(maxsize=1)

    def receive(self, message):
        if message.get("generation") == self.generation and self.replies.empty():
            self.replies.put_nowait(message)

    async def _expect(self, kind):
        reply = await asyncio.wait_for(self.replies.get(), 2)
        if reply.get("type") != kind:
            raise RuntimeError("Voice PE rejected the wake cache")
        return reply

    async def _command(self, kind, **data):
        if self.device.ws is not self.ws or self.ws is None or self.ws.closed:
            raise RuntimeError("Voice PE disconnected during wake cache transfer")
        await asyncio.wait_for(self.ws.send_json({"type": kind, "generation": self.generation, **data}), 2)

    async def send(self, identity, pcm):
        frame_bytes = CHANNELS * 2
        frames = len(pcm) // frame_bytes
        if not 0 < frames <= RATE or len(pcm) % frame_bytes:
            raise ValueError("The wake clip must be at most one second")
        try:
            async with asyncio.timeout(15):
                await self._command("ack_cache_start", cache_id=identity, samples=frames)
                await self._expect("ack_cache_ready")
                for sequence, first in enumerate(range(0, frames, PACKET_FRAMES)):
                    count = min(PACKET_FRAMES, frames - first)
                    header = HEADER.pack(b"JPLY", 1, CHANNELS, count, self.generation, sequence, first)
                    payload = pcm[first * frame_bytes:(first + count) * frame_bytes]
                    await asyncio.wait_for(self.ws.send_bytes(header + payload), 2)
                    reply = await self._expect("ack_cache_progress")
                    if reply.get("samples") != first + count:
                        raise RuntimeError("Voice PE reported an unexpected wake cache offset")
                await self._command("ack_cache_commit")
                reply = await self._expect("ack_cache_committed")
                if reply.get("cache_id") != identity:
                    raise RuntimeError("Voice PE confirmed an unexpected wake cache")
        except (OSError, RuntimeError, TimeoutError, asyncio.CancelledError):
            if self.device.ws is self.ws and self.ws and not self.ws.closed:
                # aiohttp sends in order, so an abort cannot precede the upload it cancels.
                try:
                    await self._command("ack_cache_abort")
                except (OSError, RuntimeError, TimeoutError):
                    pass
            raise


class JackPlayback:
    """One generation on the current paired socket. Never replays on reconnect."""

    def __init__(self, device, playback_id):
        self.device = device
        self.ws = device.ws
        self.id = playback_id
        self.generation = secrets.randbelow(0xFFFFFFFF) + 1
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.progress = asyncio.Event()
        self.frames_sent = 0
        self.frames_accepted = 0
        self.frames_rendered = None
        self.last_rendered_ms = None
        self.first_render_report = None
        self.accepted = []
        self.reason = None
        self.terminal_confirmed = False
        self.max_in_flight = 0
        self.max_send_lag_ms = 0.0

    def receive(self, message):
        if message.get("generation") != self.generation:
            return
        kind = message["type"]
        if kind == "play_render" and message.get("playback_id") == self.id:
            first, count, stamp = (message.get(k) for k in ("first_sample", "samples", "device_time_ms"))
            if (any(type(v) is not int for v in (first, count, stamp))
                    or first != self.frames_accepted or not 0 < count <= PACKET_FRAMES
                    or first + count > self.frames_sent or not 0 <= stamp <= 0xFFFFFFFF):
                self.reason = "invalid_render_acknowledgment"
                self.finished.set()
            else:
                self.frames_accepted += count
                self.accepted.append((first, count, stamp))
            self.progress.set()
        elif message.get("playback_id") == self.id:
            rendered = message.get("rendered_samples")
            stamp = message.get("last_rendered_ms")
            if (type(rendered) is int and (self.frames_rendered or 0) <= rendered <= self.frames_sent
                    and type(stamp) is int and 0 <= stamp <= 0xFFFFFFFF):
                self.frames_rendered = rendered
                self.last_rendered_ms = stamp
                if rendered and self.first_render_report is None:
                    self.first_render_report = rendered, stamp
            if kind == "play_started":
                self.started.set()
            elif kind in ("play_stopped", "play_drained"):
                self.reason = "drained" if kind == "play_drained" else str(message.get("reason", "stopped"))[:64]
                self.terminal_confirmed = True
                self.finished.set()
                self.progress.set()
                self.started.set()

    async def command(self, kind):
        if self.ws is not self.device.ws or self.ws is None or self.ws.closed:
            raise RuntimeError("Voice PE disconnected")
        await asyncio.wait_for(self.ws.send_json({"type": kind, "playback_id": self.id,
                                                "generation": self.generation}), 2)

    def ensure_playing(self):
        if self.finished.is_set():
            raise RuntimeError(f"Voice PE playback ended early ({self.reason})")

    async def send(self, pcm, *, prime_packets=1):
        async def chunks():
            size = RATE * CHANNELS * 2
            for offset in range(0, len(pcm), size):
                yield pcm[offset:offset + size]
        await self.send_stream(chunks(), prime_packets=prime_packets)

    async def send_stream(self, chunks, *, prime_packets=1):
        """Start once PCM exists and preserve sample timing across arriving chunks."""
        frame_bytes = CHANNELS * 2
        packet_bytes = PACKET_FRAMES * frame_bytes

        async def packets():
            partial = bytearray()
            async for chunk in chunks:
                if len(chunk) % frame_bytes:
                    raise ValueError("Playback stream contains a partial stereo frame")
                if len(chunk) > RATE * frame_bytes:
                    raise ValueError("Playback chunk exceeds the one-second bound")
                partial.extend(chunk)
                for offset in range(0, len(partial) - packet_bytes + 1, packet_bytes):
                    yield bytes(partial[offset:offset + packet_bytes])
                consumed = len(partial) // packet_bytes * packet_bytes
                del partial[:consumed]
            if partial:
                yield bytes(partial)

        if not 1 <= prime_packets <= MAX_IN_FLIGHT // PACKET_FRAMES:
            raise ValueError("Playback prime exceeds the bounded device window")
        source = packets()
        prime = deque()
        for _ in range(prime_packets):
            packet = await anext(source, None)
            if packet is None:
                break
            prime.append(packet)
        if not prime:
            raise ValueError("Playback stream is empty")
        await self.command("play_start")
        await asyncio.wait_for(self.started.wait(), 2)
        self.ensure_playing()
        anchor = time.monotonic()
        sequence = 0
        payload = prime.popleft()
        advance_frames = (prime_packets - 1) * PACKET_FRAMES
        while payload is not None:
            while self.frames_sent - self.frames_accepted >= MAX_IN_FLIGHT:
                self.progress.clear()
                self.ensure_playing()
                await asyncio.wait_for(self.progress.wait(), .15)
            due = anchor + max(0, self.frames_sent - advance_frames) / RATE
            await asyncio.sleep(max(0.0, due - time.monotonic()))
            self.ensure_playing()
            if self.ws is not self.device.ws or self.ws.closed:
                raise RuntimeError("Voice PE disconnected")
            lag = max(0.0, (time.monotonic() - due) * 1000)
            self.max_send_lag_ms = max(self.max_send_lag_ms, lag)
            if lag > 100:
                raise RuntimeError("Voice PE sender fell behind; playback stopped without acceleration")
            count = len(payload) // frame_bytes
            header = HEADER.pack(b"JPLY", 1, CHANNELS, count, self.generation, sequence, self.frames_sent)
            self.frames_sent += count
            sequence += 1
            self.max_in_flight = max(self.max_in_flight, self.frames_sent - self.frames_accepted)
            await asyncio.wait_for(self.ws.send_bytes(header + payload), .15)
            payload = prime.popleft() if prime else await anext(source, None)
        await self.command("play_drain")
        await asyncio.wait_for(self.finished.wait(), 3)
        if self.reason != "drained" or self.frames_accepted != self.frames_sent:
            raise RuntimeError(f"Voice PE playback did not drain cleanly ({self.reason})")

    async def stop(self):
        if self.terminal_confirmed or self.ws is not self.device.ws or not self.ws or self.ws.closed:
            return
        self.finished.clear()
        await self.command("play_stop")
        await asyncio.wait_for(self.finished.wait(), 2)

    def accepted_at(self, frame, clock_offset_ms):
        for first, count, stamp in self.accepted:
            if first <= frame < first + count:
                return (stamp + clock_offset_ms) / 1000 + (frame - first) / RATE
        return None

    def confirmed_span(self):
        """Return inferred DMA start and reported end for fully drained audio."""
        if (self.reason != "drained" or self.frames_rendered != self.frames_sent
                or self.first_render_report is None or self.last_rendered_ms is None):
            return None
        first_count, first_end = self.first_render_report
        first_ms = (first_end - round(first_count * 1000 / RATE)) & 0xFFFFFFFF
        if first_ms >= self.last_rendered_ms:
            return None
        return first_ms, self.last_rendered_ms

    def snapshot(self):
        result = {"frames_sent": self.frames_sent, "frames_accepted": self.frames_accepted,
                "frames_rendered": self.frames_rendered, "last_rendered_device_ms": self.last_rendered_ms,
                "max_in_flight_ms": round(self.max_in_flight / RATE * 1000, 1),
                "max_send_lag_ms": round(self.max_send_lag_ms, 1), "reason": self.reason,
                "clock_scope": "speaker acceptance and firmware output counters, not acoustic output"}
        if self.first_render_report is not None:
            first, stamp = self.first_render_report
            if self.frames_rendered > first:
                elapsed = (self.last_rendered_ms - stamp) & 0xFFFFFFFF
                expected = (self.frames_rendered - first) * 1000 / RATE
                result["output_clock_span_difference_ms"] = round(elapsed - expected, 1)
        return result
