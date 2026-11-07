"""One explicitly authorized wake-driven voice turn."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import time

import numpy as np

from .audio import RATE
from .live_bench import BenchReply
from .live_session import LiveSession
from .streaming_aec import StreamingEcho

INSTRUCTIONS = (
    "You are Jarvis, a concise British-style voice assistant. "
    "Wait for the user to finish their question, then answer briefly. "
    "Do not greet, delegate work or claim to have controlled any devices."
)


class CleanInput:
    """Bridge 16 ms device blocks to paced 20 ms input without discarding speech."""

    def __init__(self, delay_ms, pcm, first_ms, last_ms, clock_offset_ms):
        self.echo = StreamingEcho(delay_ms)
        self.echo.render(pcm, (first_ms + clock_offset_ms) / 1000, first_frame=0)
        self.echo.finish_render(len(pcm) // 4, (last_ms + clock_offset_ms) / 1000)
        self.pending = bytearray()
        self.changed = asyncio.Event()
        self.error = None
        self.capture_start = None
        self.released_samples = 0

    def offer(self, frame, clock_offset_ms):
        if self.error:
            return
        try:
            clean = self.echo.capture(frame, clock_offset_ms)
            if self.capture_start is None:
                self.capture_start = (frame.capture_ms + clock_offset_ms) / 1000
            if len(self.pending) + len(clean) > 3 * RATE * 2:
                raise RuntimeError("clean_opening_queue_overflow")
            self.pending.extend(clean)
        except (RuntimeError, ValueError):
            self.error = "opening_echo_or_queue_failed"
            self.pending.clear()
        self.changed.set()

    async def packet(self):
        while len(self.pending) < 640:
            if self.error:
                raise RuntimeError(self.error)
            self.changed.clear()
            await asyncio.wait_for(self.changed.wait(), .35)
        packet = bytes(self.pending[:640])
        del self.pending[:640]
        self.released_samples += 320
        return packet, self.capture_start + self.released_samples / RATE


class SpeechEndpoint:
    """Estimate a local speech boundary from consecutive cleaned 20 ms packets."""

    def __init__(self, detector=None):
        if detector is None:
            import webrtcvad
            detector = webrtcvad.Vad(2)
        self.detector = detector
        self.recent = deque(maxlen=5)
        self.quiet_frames = 0
        self.active = False
        self.last_voice = None
        self.voice_packets = 0

    def observe(self, pcm, captured_at, sent_at):
        voiced = self.detector.is_speech(pcm, RATE)
        self.recent.append(voiced)
        if voiced:
            self.voice_packets += 1
            self.quiet_frames = 0
            if self.active or sum(self.recent) >= 3:
                self.active = True
                self.last_voice = captured_at, sent_at
        else:
            self.quiet_frames += 1
            if self.quiet_frames >= 8:
                self.active = False

    def timing(self, first_voice_at):
        scope = "local WebRTC VAD estimate on transmitted echo-processed audio"
        if self.last_voice is None or self.quiet_frames < 8:
            return {"status": "unavailable", "scope": scope, "reason": "No qualified speech followed by 160 ms of quiet"}
        captured_at, sent_at = self.last_voice
        if not 0 <= first_voice_at - sent_at <= 2:
            return {"status": "unavailable", "scope": scope, "reason": "The speech estimate could not be paired with this reply"}
        return {"status": "estimated", "scope": scope,
                "speech_end_to_send_ms": round((sent_at - captured_at) * 1000, 1),
                "speech_end_send_to_cloud_voice_ms": round((first_voice_at - sent_at) * 1000, 1),
                "speech_end_to_cloud_voice_ms": round((first_voice_at - captured_at) * 1000, 1)}


def gain_packet(pcm, gain_db):
    """Apply fixed gain after AEC and report overload without auto-gain pumping."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
    scaled = np.rint(samples * 10 ** (gain_db / 20)).astype(np.int32)
    clipped = int(np.count_nonzero((scaled < -32768) | (scaled > 32767)))
    output = np.clip(scaled, -32768, 32767).astype("<i2")
    return output.tobytes(), int(np.max(np.abs(samples))), int(np.max(np.abs(output.astype(np.int32)))), clipped


async def run_voice_turn(device, settings, result, opening, pcm, delay_ms, phase, *, gain_db=0):
    """Open on wake, clean the local acknowledgment, then stream one reply."""
    reply = BenchReply(device, result["id"])
    ws = device.ws
    sender = startup = None
    accept_output = False
    endpoint = SpeechEndpoint()
    sent = peak_before = peak_after = clipped = 0
    first_age = max_age = None

    async def on_audio(audio, rate):
        if accept_output:
            await reply.receive(audio, rate)

    session = LiveSession(settings, on_audio=on_audio, transport="websocket",
                          max_seconds=30, startup_timeout=3, instructions=INSTRUCTIONS)
    try:
        phase("connecting to GPT-Live while the cached acknowledgment plays")
        started = time.monotonic()
        startup = asyncio.create_task(session.start())
        async with asyncio.timeout(3):
            await startup
            result["wake_receipt_to_ready_ms"] = round((time.monotonic() - started) * 1000, 1)
            await device.wake_terminal_event.wait()
        terminal = device.wake_playback_terminal
        first, last = terminal.get("first_rendered_ms"), terminal.get("last_rendered_ms")
        if (not terminal.get("drained") or terminal.get("rendered_samples") != len(pcm) // 4
                or type(first) is not int or type(last) is not int):
            raise RuntimeError("wake_echo_reference_incomplete")
        buffered, result["opening_audio"] = opening.release(time.monotonic())
        if not buffered or device.errors:
            raise RuntimeError(result["opening_audio"]["reason"] or "opening_capture_failed")
        stream = CleanInput(delay_ms, pcm, first, last, device.clock_offset_ms)
        for frame in buffered:
            stream.offer(frame, device.clock_offset_ms)
        device.pcm_consumer = stream.offer
        device.playback = reply.player
        # The extra source reserve remains on the App. The device window is
        # still 50 ms and playback keeps the original sample clock.
        result["reply_source_reserve_ms"] = 120
        sender = asyncio.create_task(reply.player.send_stream(reply.chunks(), prime_packets=5, source_prime_packets=12))
        phase("streaming the cleaned opening speech")
        accept_output = True
        origin = time.monotonic()
        while reply.first_voice_at is None:
            if time.monotonic() - origin >= 10:
                raise TimeoutError("No spoken reply within the one-turn input window")
            if device.errors or session.metrics.error or sender.done() or not device.info.get("jack_plugged"):
                raise RuntimeError("The microphone, cloud session or jack playback failed")
            packet, captured_at = await stream.packet()
            due = origin + sent / RATE
            await asyncio.sleep(max(0, due - time.monotonic()))
            if time.monotonic() - due > .1:
                raise RuntimeError("Microphone input pacing exceeded 100 ms")
            if reply.first_voice_at is None:
                sent_at = time.monotonic()
                age = (sent_at - captured_at) * 1000
                if not -100 <= age <= opening.max_ms:
                    raise RuntimeError("Microphone input exceeded its capture-age bound; retry the turn")
                first_age = age if first_age is None else first_age
                max_age = age if max_age is None else max(max_age, age)
                packet, before, after, overload = gain_packet(packet, gain_db)
                await session.send_audio(packet)
                peak_before = max(peak_before, before)
                peak_after = max(peak_after, after)
                clipped += overload
                endpoint.observe(packet, captured_at, sent_at)
                sent += 320
        device.pcm_consumer = None
        result["speech_endpoint"] = endpoint.timing(reply.first_voice_at)
        result["digital_first_voice_ms"] = round((reply.first_voice_at - origin) * 1000, 1)
        phase("playing the GPT-Live reply through the jack")
        async with asyncio.timeout(8):
            while time.monotonic() - reply.last_voice_at < .6:
                if sender.done():
                    sender.result()
                    raise RuntimeError("Reply playback stopped early")
                if session.metrics.error:
                    raise RuntimeError("GPT-Live stopped before the reply finished")
                await asyncio.sleep(.025)
            accept_output = False
            await reply.finish()
            await sender
        if reply.player.frames_rendered != reply.player.frames_sent:
            raise RuntimeError("Voice PE did not confirm the complete reply")
        result["playback"] = reply.player.snapshot()
        return reply
    finally:
        result["input_samples_sent"] = sent
        result["input_queue"] = {"first_packet_age_ms": round(first_age, 1) if first_age is not None else None,
                                 "max_packet_age_ms": round(max_age, 1) if max_age is not None else None}
        result["input_signal"] = {"gain_db": gain_db, "peak_before_gain_pcm": peak_before,
                                  "peak_transmitted_pcm": peak_after, "clipped_samples": clipped,
                                  "vad_voice_packets": endpoint.voice_packets}
        device.pcm_consumer = None
        if startup is not None and not startup.done():
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
        if sender is not None:
            if not sender.done():
                sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            with suppress(RuntimeError, OSError, TimeoutError):
                await reply.player.stop()
            result["playback"] = reply.player.snapshot()
        device.playback = None
        wake = device.wake_details
        if (device.ws is ws and ws is not None and not ws.closed and wake.get("ack_playback_id")
                and not device.wake_terminal_event.is_set()):
            with suppress(OSError, RuntimeError, TimeoutError):
                await asyncio.wait_for(ws.send_json({"type": "play_stop",
                    "playback_id": wake["ack_playback_id"], "generation": wake["ack_generation"]}), 1)
        metrics = await session.close()
        result["cloud"] = {"transport": "websocket", "profile": "one-turn preview",
                           "startup_ms": round(metrics.ready_ms, 1) if metrics.ready_ms is not None else None,
                           "finalization_confirmed": metrics.finalization_confirmed,
                           "usage_seconds": metrics.usage_seconds}
