"""Generate a bounded greeting asset without accessing the room microphone."""

import asyncio
import os
import re
import time

import numpy as np

from .live_bench import voiced_bounds
from .live_session import LiveSession, MODEL, RATE, VOICE


PHRASE = "Yes sir"
GREETING = 'Say exactly "Yes sir" once, then stay silent. Do not add any other words.'


def cache_path(data_dir):
    return data_dir / f"ack-{MODEL}-{VOICE}-yes-sir.pcm"


async def generate_acknowledgment(settings, data_dir):
    received = bytearray()
    transcript = []
    last_voice = None

    async def on_audio(pcm, rate):
        nonlocal last_voice
        if rate != RATE or len(pcm) % 2 or len(received) + len(pcm) > 12 * RATE * 2:
            raise ValueError("Greeting audio exceeded its format or duration bound")
        received.extend(pcm)
        if voiced_bounds(np.frombuffer(pcm, dtype="<i2")) is not None:
            last_voice = time.monotonic()

    def on_transcript(delta):
        if not isinstance(delta, str) or len(delta) + sum(map(len, transcript)) > 80:
            raise ValueError("Greeting transcript exceeded its bound")
        transcript.append(delta)

    session = LiveSession(settings, on_audio=on_audio, transport="websocket", max_seconds=30,
                          greeting=GREETING, on_transcript=on_transcript)
    try:
        await session.start()
        await session.request_greeting()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if session.metrics.error:
                raise RuntimeError("Greeting generation failed")
            await session.send_audio(bytes(640))
            await asyncio.sleep(.02)
            if last_voice is not None and time.monotonic() - last_voice >= 1.25:
                break
        else:
            raise RuntimeError("Greeting generation did not finish within the observation window")
    finally:
        metrics = await session.close()
    if not metrics.finalization_confirmed or metrics.error:
        raise RuntimeError("Greeting session did not close cleanly")
    words = re.findall(r"[a-z]+", "".join(transcript).casefold())
    if words != ["yes", "sir"]:
        raise ValueError("Generated greeting transcript did not match Yes sir")
    samples = np.frombuffer(received, dtype="<i2")
    bounds = voiced_bounds(samples)
    if bounds is None or not RATE // 8 <= bounds[1] - bounds[0] <= 2 * RATE:
        raise ValueError("Generated greeting did not contain a usable short clip")
    start, end = bounds
    pcm = samples[max(0, start - RATE // 50):end + RATE // 25].tobytes()
    path = cache_path(data_dir)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(pcm)
    os.replace(temporary, path)
    return {"model": MODEL, "voice": VOICE, "phrase": PHRASE,
            "duration_ms": round(len(pcm) / (2 * RATE) * 1000),
            "verification": "generated transcript matched; listening check pending"}
