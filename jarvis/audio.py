"""Known probes and conservative acoustic detection; no cloud or audio persistence."""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass

import numpy as np

RATE = 16000
FRAME = struct.Struct("<4sBBHIQI")


@dataclass(frozen=True)
class PCMFrame:
    sequence: int
    first_sample: int
    capture_ms: int
    samples: np.ndarray


def decode_frame(data: bytes) -> PCMFrame:
    if len(data) < FRAME.size:
        raise ValueError("PCM frame is shorter than its header")
    magic, version, channels, count, seq, first, stamp = FRAME.unpack_from(data)
    if magic != b"JPCM" or version != 1 or channels != 1 or count == 0 or count > 4096:
        raise ValueError("Unsupported PCM frame header")
    if len(data) != FRAME.size + count * 2:
        raise ValueError("PCM payload length does not match header")
    return PCMFrame(seq, first, stamp, np.frombuffer(data, dtype="<i2", offset=FRAME.size).copy())


def make_probe(seed: int = 0) -> np.ndarray:
    """Nine seeded chirp symbols; distinct trials cannot match adjacent probes."""
    rng = np.random.default_rng(seed)
    parts = []
    for _ in range(9):
        t = np.arange(int(RATE * .06)) / RATE
        f0 = float(rng.choice([650, 940, 1270, 1630, 2020, 2450, 2950]))
        f1 = f0 + rng.uniform(-100, 100)
        phase = 2 * np.pi * (f0 * t + (f1 - f0) * t * t / (.12))
        envelope = np.sin(np.linspace(0, np.pi, len(t))) ** 2
        parts.extend([.12 * np.sin(phase) * envelope, np.zeros(int(RATE * .025))])
    return (np.concatenate(parts) * 32767).astype("<i2")


def wav_header(rate: int = RATE) -> bytes:
    # Streaming WAV uses a conventional unknown/very large data length. Some
    # receivers reject this framing; that is a capability failure, not success.
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 0xFFFFFFFF, b"WAVE",
                       b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16, b"data", 0xFFFFFFFF)


def _features(pcm: np.ndarray) -> np.ndarray:
    n, hop = 256, 64
    signal = pcm.astype(np.float64) / 32768
    if len(signal) < n:
        return np.empty((0, 12))
    frames = np.lib.stride_tricks.sliding_window_view(signal, n)[::hop]
    power = np.abs(np.fft.rfft(frames * np.hanning(n), axis=1)) ** 2
    edges = np.linspace(5, 55, 13).astype(int)
    return np.stack([np.sqrt(power[:, a:b].sum(axis=1)) for a, b in zip(edges[:-1], edges[1:])], axis=1)


def detect_probe(captured: np.ndarray, probe: np.ndarray) -> dict:
    """Spectral normalized matching tolerates independent audio clock phase.

    Report no result for clipped, ambiguous or absent signals. Offset is from
    capture start; clock and microphone pipeline uncertainty is added by caller.
    """
    if len(captured) < len(probe) + 256:
        return {"status": "unavailable", "reason": "Capture is too short", "confidence": 0.0}
    if np.mean(np.abs(captured.astype(float)) >= 32760) > .005:
        return {"status": "unavailable", "reason": "Microphone clipped", "confidence": 0.0}
    a, b = _features(captured), _features(probe)
    length = len(a) + len(b) - 1
    fft_len = 1 << (length - 1).bit_length()
    corr = np.zeros(len(a) - len(b) + 1)
    for band in range(b.shape[1]):
        c = np.fft.irfft(np.fft.rfft(a[:, band], fft_len) * np.fft.rfft(b[::-1, band], fft_len), fft_len)
        corr += c[len(b) - 1:len(a)]
    energy = np.sum(a * a, axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(energy)))
    denom = np.sqrt(np.maximum(0, cumulative[len(b):] - cumulative[:-len(b)]) * np.sum(b * b))
    score = np.divide(corr, denom, out=np.zeros_like(corr), where=denom > 1e-9)
    peak = int(np.argmax(score))
    confidence = min(1.0, max(0.0, float(score[peak])))
    alternatives = score.copy()
    exclusion = int(.25 * RATE / 64)
    alternatives[max(0, peak - exclusion):peak + exclusion + 1] = 0
    competing = float(np.max(alternatives))
    if confidence < .65 or competing > confidence - .045:
        return {"status": "unavailable", "reason": "Absent or ambiguous acoustic probe", "confidence": confidence}
    return {"status": "detected", "offset_seconds": peak * 64 / RATE, "confidence": confidence,
            "resolution_ms": 4.0, "method": "normalized spectral sequence match"}


def summarize(trials: list[dict]) -> dict:
    values = [t["latency_ms"] for t in trials if t.get("status") == "detected" and t.get("latency_ms") is not None]
    return {"valid_trials": len(values), "total_trials": len(trials),
            "median_ms": round(float(np.median(values)), 1) if values else None,
            "p95_ms": round(float(np.percentile(values, 95)), 1) if values else None,
            "percentile_is_estimate": True}


def detect_stop(captured: np.ndarray, capture_start: float, stop_at: float) -> dict:
    """Detect cessation of the two-tone pilot, including acoustic decay.

    Requires stable pilot before stop and sustained quiet afterward. This does
    not use total microphone level, which would confuse user speech with echo.
    """
    n, hop = 512, 80
    if len(captured) < RATE * 2:
        return {"status": "unavailable", "latency_ms": None, "reason": "Capture too short for a stop measurement"}
    frames = np.lib.stride_tricks.sliding_window_view(captured.astype(float) / 32768, n)[::hop]
    power = np.abs(np.fft.rfft(frames * np.hanning(n), axis=1)) ** 2
    bands = []
    for hz in (937, 1579):
        index = int(round(hz * n / RATE))
        bands.append(power[:, index - 1:index + 2].sum(axis=1))
    total = power[:, 10:100].sum(axis=1) + 1e-12
    target = bands[0] + bands[1]
    times = capture_start + (np.arange(len(frames)) * hop + n / 2) / RATE
    baseline = float(np.percentile(target[:max(1, int(.4 * RATE / hop))], 90))
    active = (target / total > .6) & (bands[0] / total > .1) & (bands[1] / total > .1) & (target > max(baseline * 6, .001))
    before = active & (times < stop_at) & (times > stop_at - 1)
    if np.count_nonzero(before) < int(.25 * RATE / hop):
        return {"status": "unavailable", "latency_ms": None, "reason": "No stable acoustic pilot before stop; player may have started too late"}
    indices = np.flatnonzero(active & (times > stop_at - .2))
    if not len(indices):
        return {"status": "unavailable", "latency_ms": None, "reason": "Stop boundary could not be identified"}
    last = int(indices[-1])
    if times[-1] - times[last] < .5:
        return {"status": "unavailable", "latency_ms": None, "reason": "Pilot was still audible at capture end"}
    latency = (times[last] - stop_at) * 1000
    if latency < -40:
        return {"status": "unavailable", "latency_ms": None, "reason": "Pilot ended before stop command"}
    return {"status": "detected", "latency_ms": round(max(0, latency), 1),
            "confidence": round(float(np.median((target / total)[before])), 3),
            "method": "two-band acoustic pilot cessation", "resolution_ms": 32.0}


def detect_stability(captured, capture_start, stream_start, stream_duration, latency_ms, markers):
    """Inspect continuous pilot between the coded probes in a quiet room."""
    n, hop = 512, 80
    frames = np.lib.stride_tricks.sliding_window_view(captured.astype(float) / 32768, n)[::hop]
    power = np.abs(np.fft.rfft(frames * np.hanning(n), axis=1)) ** 2
    total = power[:, 10:110].sum(axis=1) + 1e-12
    target = np.zeros(len(frames))
    for hz in (937, 1579):
        index = int(round(hz * n / RATE))
        target += power[:, index - 1:index + 2].sum(axis=1)
    times = capture_start + (np.arange(len(frames)) * hop + n / 2) / RATE
    lag = latency_ms / 1000
    mask = (times > stream_start + lag + 1) & (times < stream_start + lag + stream_duration - 1)
    for emitted, probe in markers:
        mask &= ~((times > emitted + lag - .1) & (times < emitted + lag + len(probe) / RATE + .1))
    active = (target / total > .55) & (target > .001)
    if np.count_nonzero(mask) < RATE / hop / 2 or np.count_nonzero(active & mask) < RATE / hop / 4:
        return {"status": "unavailable", "reason": "Continuous pilot could not be established independently of probes"}
    gaps, run = [], 0
    for valid, heard in zip(mask, active):
        if valid and not heard:
            run += 1
        else:
            if run >= 10: gaps.append(run * hop / RATE * 1000)
            run = 0
    if run >= 10: gaps.append(run * hop / RATE * 1000)
    return {"status": "measured", "dropout_count": len(gaps),
            "dropout_ms": round(sum(gaps), 1), "longest_gap_ms": round(max(gaps, default=0), 1),
            "observed_ms": round(np.count_nonzero(mask) * hop / RATE * 1000, 1),
            "method": "continuous two-band pilot outside probe windows; quiet-room estimate"}
