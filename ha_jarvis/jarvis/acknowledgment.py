"""Generate a bounded greeting asset without accessing the room microphone."""

import asyncio
import hashlib
import io
import os
import re
import time
import wave

import numpy as np

from .live_bench import voiced_bounds
from .live_session import LiveSession, MODEL, RATE, VOICE
from .playback import stereo_pcm


PHRASE = "Yes sir. How can I help?"
GREETING = (f'Say exactly "{PHRASE}" once. Use a calm, natural cadence with a short pause '
            'after "sir", then stay silent. Do not add any other words.')
WAKE_PAUSE_MS = 250
GREETING_OUTPUT_GAIN_DB = -10
MAX_CLIP_SECONDS = 3


def cache_path(data_dir):
    return data_dir / f"ack-{MODEL}-{VOICE}-how-can-i-help.pcm"


def read_candidate(data_dir):
    with cache_path(data_dir).open("rb") as file:
        pcm = file.read(2 * RATE * MAX_CLIP_SECONDS + 1)
    if not RATE // 4 <= len(pcm) <= 2 * RATE * MAX_CLIP_SECONDS or len(pcm) % 2:
        raise ValueError("Cached acknowledgment has an invalid length")
    return pcm


def candidate_wav(pcm):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        wav.writeframes(greeting_playback_samples(pcm).tobytes())
    return output.getvalue()


def candidate_identity(pcm):
    return hashlib.sha256(pcm).hexdigest()


def greeting_playback_identity(candidate_id):
    return hashlib.sha256(f"{candidate_id}:gain:{GREETING_OUTPUT_GAIN_DB}".encode("ascii")).hexdigest()


def greeting_playback_samples(pcm):
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    return np.rint(samples * 10 ** (GREETING_OUTPUT_GAIN_DB / 20)).astype("<i2")


def approval_path(data_dir):
    return cache_path(data_dir).with_suffix(".verified")


def verified_render(data_dir):
    path = approval_path(data_dir)
    if not path.is_file() or not cache_path(data_dir).is_file():
        return None
    pcm = read_candidate(data_dir)
    ident = candidate_identity(pcm)
    if path.read_text(encoding="ascii").strip() != ident:
        return None
    return greeting_playback_identity(ident), stereo_pcm(greeting_playback_samples(pcm))


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
    if words != re.findall(r"[a-z]+", PHRASE.casefold()):
        raise ValueError(f"Generated greeting transcript did not match {PHRASE}")
    samples = np.frombuffer(received, dtype="<i2")
    bounds = voiced_bounds(samples)
    if bounds is None or bounds[1] - bounds[0] < RATE // 8:
        raise ValueError("Generated greeting did not contain a usable short clip")
    start, end = bounds
    lead_in = bytes(2 * (RATE * WAKE_PAUSE_MS // 1000))
    pcm = lead_in + samples[max(0, start - RATE // 50):end + RATE // 25].tobytes()
    if len(pcm) > 2 * RATE * MAX_CLIP_SECONDS:
        raise ValueError("Generated greeting was too long for the Voice PE cache")
    path = cache_path(data_dir)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(pcm)
    os.replace(temporary, path)
    return {"model": MODEL, "voice": VOICE, "phrase": PHRASE,
            "duration_ms": round(len(pcm) / (2 * RATE) * 1000),
            "verification": "generated transcript matched; listening check pending"}
