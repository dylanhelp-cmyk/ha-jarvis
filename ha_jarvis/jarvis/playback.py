"""Bounded PCM sender for the Voice PE jack."""
from __future__ import annotations

import asyncio
from collections import deque
import secrets
import struct
import time

import numpy as np


RATE = 48000
VOICE_OUTPUT_GAIN_DB = -6
CHANNELS = 2
PACKET_FRAMES = 480
HEADER = struct.Struct("<4sBBHIIQ")
MAX_IN_FLIGHT = 6 * PACKET_FRAMES
# PCM ahead of playback is separate from packets awaiting device acceptance.
MAX_PRIME_PACKETS = 10
MAX_SEND_LAG_MS = 100
MAX_REPLY_RECOVERY_LAG_MS = 500
WAKE_CACHE_WINDOW = 4


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
        self.replies = asyncio.Queue(maxsize=WAKE_CACHE_WINDOW)

    def receive(self, message):
        if message.get("generation") == self.generation and not self.replies.full():
            self.replies.put_nowait(message)

    async def _expect(self, kind):
        reply = await asyncio.wait_for(self.replies.get(), 2)
        if reply.get("type") != kind:
            if reply.get("type") == "ack_cache_error":
                reason = reply.get("reason")
                if reason in ("invalid_manifest", "busy", "no_memory", "unknown_generation",
                              "incomplete_upload", "queue_overflow", "upload_timeout", "invalid_sequence"):
                    raise RuntimeError(f"Voice PE rejected the wake cache: {reason}")
            raise RuntimeError("Voice PE rejected the wake cache")
        return reply

    async def _command(self, kind, **data):
        if self.device.ws is not self.ws or self.ws is None or self.ws.closed:
            raise RuntimeError("Voice PE disconnected during wake cache transfer")
        await asyncio.wait_for(self.ws.send_json({"type": kind, "generation": self.generation, **data}), 2)

    async def send(self, identity, pcm):
        frame_bytes = CHANNELS * 2
        frames = len(pcm) // frame_bytes
        max_frames = self.device.info.get("ack_cache_max_frames", RATE)
        if (type(max_frames) is not int or not 0 < frames <= min(max_frames, 3 * RATE)
                or len(pcm) % frame_bytes):
            raise ValueError("The wake clip exceeds this Voice PE firmware's cache capacity")
        try:
            async with asyncio.timeout(15):
                await self._command("ack_cache_start", cache_id=identity, samples=frames)
                await self._expect("ack_cache_ready")
                window_frames = WAKE_CACHE_WINDOW * PACKET_FRAMES
                for window_start in range(0, frames, window_frames):
                    offsets = range(window_start, min(window_start + window_frames, frames), PACKET_FRAMES)
                    for first in offsets:
                        count = min(PACKET_FRAMES, frames - first)
                        header = HEADER.pack(b"JPLY", 1, CHANNELS, count, self.generation,
                                             first // PACKET_FRAMES, first)
                        payload = pcm[first * frame_bytes:(first + count) * frame_bytes]
                        await asyncio.wait_for(self.ws.send_bytes(header + payload), 2)
                    for first in offsets:
                        reply = await self._expect("ack_cache_progress")
                        if reply.get("samples") != min(first + PACKET_FRAMES, frames):
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
        self.on_render_progress = None
        self.frames_sent = 0
        self.frames_accepted = 0
        self.frames_rendered = None
        self.last_rendered_ms = None
        self.first_render_report = None
        self.first_rendered_ms = None
        self.max_render_discontinuity_us = None
        self.render_clock_invalid = False
        self.accepted = []
        self.reason = None
        self.terminal_confirmed = False
        self.max_in_flight = 0
        self.max_send_lag_ms = 0.0
        self.schedule_rebases = 0
        self.schedule_shift_ms = 0.0
        self.max_source_wait_ms = 0.0
        self.source_prime_wait_ms = None
        self.play_start_wait_ms = None
        # Keep numeric sender timings for recent packets only. One DMA report
        # normally covers 100 ms; older samples can have unavailable coverage.
        self.recent_sends = deque(maxlen=128)
        self.first_clock_gap = None

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
            advanced = False
            if (type(rendered) is int and (self.frames_rendered or 0) <= rendered <= self.frames_sent
                    and type(stamp) is int and 0 <= stamp <= 0xFFFFFFFF):
                advanced = rendered > (self.frames_rendered or 0)
                previous_count, previous_end = self.frames_rendered or 0, self.last_rendered_ms
                self.frames_rendered = rendered
                self.last_rendered_ms = stamp
                if rendered:
                    if self.first_render_report is None:
                        self.first_render_report = rendered, stamp
                    if (self.first_rendered_ms is not None or "first_rendered_ms" in message
                            or "max_render_discontinuity_us" in message):
                        first = message.get("first_rendered_ms")
                        gap = message.get("max_render_discontinuity_us")
                        if (type(first) is not int or not 0 <= first <= stamp
                                or type(gap) is not int or not 0 <= gap <= 120_000_000
                                or (self.first_rendered_ms is not None and first != self.first_rendered_ms)
                                or gap < (self.max_render_discontinuity_us or 0)):
                            self.render_clock_invalid = True
                        else:
                            self.first_rendered_ms = first
                            self.max_render_discontinuity_us = gap
                            if advanced and self.first_clock_gap is None and (
                                    gap > 15_000 or abs(stamp - first - rendered * 1000 / RATE) > 15):
                                self.first_clock_gap = self._clock_gap(previous_count, previous_end, rendered, stamp)
            if kind == "play_clock" and advanced and self.on_render_progress is not None:
                self.on_render_progress()
            if kind == "play_started":
                self.started.set()
            elif kind in ("play_stopped", "play_drained"):
                self.reason = "drained" if kind == "play_drained" else str(message.get("reason", "stopped"))[:64]
                self.terminal_confirmed = True
                self.finished.set()
                self.progress.set()
                self.started.set()

    def _clock_gap(self, begin, previous_end, end, end_ms):
        """Relate the first bad DMA interval to the corresponding wire packets."""
        start_ms = self.first_rendered_ms if previous_end is None else previous_end
        ms_per_frame = 1000 / RATE
        report = {"first_frame": begin, "end_frame": end,
                  "interval_excess_ms": round(end_ms - start_ms - (end - begin) * ms_per_frame, 1)}
        accepted = [(index, first, stamp) for index, (first, count, stamp) in enumerate(self.accepted)
                    if first < end and first + count > begin]
        report["accepted_packets_observed"] = len(accepted)
        if not accepted:
            return report
        # Acknowledgments timestamp the full packet entering the speaker buffer.
        # Both these values are on the device clock (no network offset estimate).
        _, first, stamp = min(accepted, key=lambda item: (item[1] - begin) * ms_per_frame - item[2])
        report.update(least_acceptance_lead_frame=first, least_acceptance_lead_ms=round(
            start_ms + (first - begin) * ms_per_frame - stamp, 1))
        intervals = [(stamp - self.accepted[index - 1][2], first) for index, first, stamp in accepted if index]
        if intervals:
            report["max_acceptance_interval_ms"], report["max_acceptance_interval_frame"] = max(intervals)
        sends = [entry for entry in self.recent_sends if accepted[0][1] <= entry[0] <= accepted[-1][1]]
        if len(sends) == len(accepted):
            longest = max(sends, key=lambda item: item[1])
            report.update(max_send_interval_ms=round(longest[1], 1), max_send_interval_frame=longest[0],
                          max_credit_wait_ms=round(max(item[2] for item in sends), 1),
                          max_source_wait_ms=round(max(item[3] for item in sends), 1))
        return report

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

    async def send_stream(self, chunks, *, prime_packets=1, source_prime_packets=None,
                          recover_reply_stalls=False):
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

        if not 1 <= prime_packets <= MAX_PRIME_PACKETS:
            raise ValueError("Playback prime exceeds 100 ms")
        source_prime = prime_packets if source_prime_packets is None else source_prime_packets
        if not prime_packets <= source_prime <= 20:
            raise ValueError("Playback source prime exceeds 200 ms")
        source = packets()
        prime = deque()
        first_packet_at = None
        start_requested = False
        for _ in range(source_prime):
            packet = await anext(source, None)
            if packet is None:
                break
            prime.append(packet)
            if first_packet_at is None:
                first_packet_at = time.monotonic()
                if source_prime > prime_packets:
                    # Let the Voice PE start its speaker while the App fills
                    # the source reserve. No PCM is sent until both are ready.
                    await self.command("play_start")
                    start_requested = True
            self.ensure_playing()
        if not prime:
            raise ValueError("Playback stream is empty")
        prime_ready_at = time.monotonic()
        self.source_prime_wait_ms = (prime_ready_at - first_packet_at) * 1000
        if not start_requested:
            await self.command("play_start")
        await asyncio.wait_for(self.started.wait(), 2)
        self.ensure_playing()
        anchor = time.monotonic()
        self.play_start_wait_ms = (anchor - prime_ready_at) * 1000
        sequence = 0
        payload = prime.popleft()
        advance_frames = (prime_packets - 1) * PACKET_FRAMES
        last_send_at = None
        source_wait_ms = 0.0
        while payload is not None:
            credit_wait_at = time.monotonic()
            while self.frames_sent - self.frames_accepted >= MAX_IN_FLIGHT:
                self.progress.clear()
                self.ensure_playing()
                await asyncio.wait_for(self.progress.wait(), .15)
            credit_wait_ms = (time.monotonic() - credit_wait_at) * 1000
            due = anchor + max(0, self.frames_sent - advance_frames) / RATE
            await asyncio.sleep(max(0.0, due - time.monotonic()))
            self.ensure_playing()
            if self.ws is not self.device.ws or self.ws.closed:
                raise RuntimeError("Voice PE disconnected")
            lag = max(0.0, (time.monotonic() - due) * 1000)
            self.max_send_lag_ms = max(self.max_send_lag_ms, lag)
            if lag > MAX_SEND_LAG_MS:
                if not recover_reply_stalls or lag > MAX_REPLY_RECOVERY_LAG_MS:
                    raise RuntimeError("Voice PE sender fell behind; playback stopped without acceleration")
                # Refill the existing device prime as data becomes available.
                # The wire can burst within its window while the DAC stays at 48 kHz.
                refill_advance_ms = min(self.frames_sent, advance_frames) * 1000 / RATE
                shift_ms = lag - refill_advance_ms
                anchor += shift_ms / 1000
                self.schedule_rebases += 1
                self.schedule_shift_ms += shift_ms
            count = len(payload) // frame_bytes
            header = HEADER.pack(b"JPLY", 1, CHANNELS, count, self.generation, sequence, self.frames_sent)
            sent_at = time.monotonic()
            interval_ms = 0.0 if last_send_at is None else (sent_at - last_send_at) * 1000
            self.recent_sends.append((self.frames_sent, interval_ms, credit_wait_ms, source_wait_ms))
            last_send_at = sent_at
            self.frames_sent += count
            sequence += 1
            self.max_in_flight = max(self.max_in_flight, self.frames_sent - self.frames_accepted)
            await asyncio.wait_for(self.ws.send_bytes(header + payload), .15)
            if prime:
                payload = prime.popleft()
                source_wait_ms = 0.0
            else:
                waiting_at = time.monotonic()
                payload = await anext(source, None)
                if payload is not None:
                    source_wait_ms = (time.monotonic() - waiting_at) * 1000
                    self.max_source_wait_ms = max(self.max_source_wait_ms, source_wait_ms)
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
        if (self.render_clock_invalid or self.reason != "drained" or self.frames_rendered != self.frames_sent
                or self.first_render_report is None or self.last_rendered_ms is None):
            return None
        first_count, first_end = self.first_render_report
        first_ms = self.first_rendered_ms
        if first_ms is None:
            first_ms = (first_end - round(first_count * 1000 / RATE)) & 0xFFFFFFFF
        if first_ms >= self.last_rendered_ms:
            return None
        return first_ms, self.last_rendered_ms

    def snapshot(self):
        result = {"frames_sent": self.frames_sent, "frames_accepted": self.frames_accepted,
                "frames_rendered": self.frames_rendered, "last_rendered_device_ms": self.last_rendered_ms,
                "max_in_flight_ms": round(self.max_in_flight / RATE * 1000, 1),
                "max_send_lag_ms": round(self.max_send_lag_ms, 1),
                "schedule_rebases": self.schedule_rebases,
                "schedule_shift_ms": round(self.schedule_shift_ms, 1),
                "max_source_wait_ms": round(self.max_source_wait_ms, 1), "reason": self.reason,
                "clock_scope": "speaker acceptance and firmware output counters, not acoustic output"}
        if self.source_prime_wait_ms is not None:
            result["source_prime_wait_ms"] = round(self.source_prime_wait_ms, 1)
        if self.first_clock_gap is not None:
            result["first_clock_gap"] = self.first_clock_gap
        if self.play_start_wait_ms is not None:
            result["play_start_wait_after_prime_ms"] = round(self.play_start_wait_ms, 1)
        if self.first_rendered_ms is not None or self.render_clock_invalid:
            result["render_clock_valid"] = not self.render_clock_invalid
            if not self.render_clock_invalid:
                result["first_rendered_device_ms"] = self.first_rendered_ms
                result["max_render_discontinuity_us"] = self.max_render_discontinuity_us
        if self.first_render_report is not None:
            first, stamp = self.first_render_report
            if self.frames_rendered > first:
                elapsed = (self.last_rendered_ms - stamp) & 0xFFFFFFFF
                expected = (self.frames_rendered - first) * 1000 / RATE
                result["output_clock_span_difference_ms"] = round(elapsed - expected, 1)
        return result
