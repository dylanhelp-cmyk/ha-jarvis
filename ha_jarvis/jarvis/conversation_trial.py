"""One explicitly authorized wake-driven voice turn."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import time

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

    def offer(self, frame, clock_offset_ms):
        if self.error:
            return
        try:
            clean = self.echo.capture(frame, clock_offset_ms)
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
        return packet


async def run_voice_turn(device, settings, result, opening, pcm, delay_ms, phase):
    """Open on wake, clean the local acknowledgment, then stream one reply."""
    reply = BenchReply(device, result["id"])
    ws = device.ws
    sender = startup = None
    accept_output = False

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
        sender = asyncio.create_task(reply.player.send_stream(reply.chunks(), prime_packets=5))
        phase("streaming the cleaned opening speech")
        accept_output = True
        origin = time.monotonic()
        sent = 0
        while reply.first_voice_at is None:
            if time.monotonic() - origin >= 10:
                raise TimeoutError("No spoken reply within the one-turn input window")
            if device.errors or session.metrics.error or sender.done() or not device.info.get("jack_plugged"):
                raise RuntimeError("The microphone, cloud session or jack playback failed")
            packet = await stream.packet()
            due = origin + sent / RATE
            await asyncio.sleep(max(0, due - time.monotonic()))
            if time.monotonic() - due > .1:
                raise RuntimeError("Microphone input pacing exceeded 100 ms")
            if reply.first_voice_at is None:
                await session.send_audio(packet)
                sent += 320
        device.pcm_consumer = None
        result["input_samples_sent"] = sent
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
