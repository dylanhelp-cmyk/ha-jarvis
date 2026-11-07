"""Reusable digital voice and acoustic matching helpers."""
from __future__ import annotations

import av
import numpy as np

from .audio import RATE, detect_probe
from .playback import RATE as RENDER_RATE


POST_INPUT_SECONDS = 15.0


def voiced_bounds(samples):
    """Conservative digital energy bounds, never a human speech endpoint."""
    samples = np.asarray(samples, dtype=np.int16)
    if not len(samples):
        return None
    padded = np.pad(samples.astype(float), (0, (-len(samples)) % 160))
    energy = np.sqrt(np.mean(padded.reshape(-1, 160) ** 2, axis=1))
    active = np.flatnonzero(energy >= max(100.0, float(np.max(energy)) * .04))
    if not len(active):
        return None
    return int(active[0] * 160), min(len(samples), int((active[-1] + 1) * 160))


def render_reference(pcm):
    """Reconstruct the mono reference from the stereo PCM sent to the Voice PE jack."""
    stereo = np.frombuffer(pcm, dtype="<i2").reshape(-1, 2)
    # Jarvis duplicates its mono input into both channels.
    mono = stereo[:, 0].astype(np.int16).reshape(1, -1)
    frame = av.AudioFrame.from_ndarray(mono, format="s16", layout="mono")
    frame.sample_rate = RENDER_RATE
    converter = av.AudioResampler(format="s16", layout="mono", rate=RATE)
    converted = converter.resample(frame) + converter.resample(None)
    return np.concatenate([block.to_ndarray().reshape(-1) for block in converted])


def match_response(captured, capture_start, reference, voiced_start_sample,
                   fixture_end_at, earliest_output_at, *, handoff_at=None,
                   acoustic_delay_range=(0.0, 2.5), window_seconds=.32):
    """Require distinct reply windows to agree before reporting onset."""
    unavailable = {"status": "unavailable", "latency_ms": None,
                   "fixture_end_to_audible_ms": None, "windows": []}
    width = int(window_seconds * RATE)
    bounds = voiced_bounds(reference)
    if bounds is None or bounds[1] - voiced_start_sample < width + int(min(.2, window_seconds) * RATE):
        return {**unavailable, "reason": "Reply is too short or weak for two distinct acoustic windows"}
    # Start windows at distinct positions over at most the first four seconds.
    last = min(bounds[1] - width, voiced_start_sample + 4 * RATE)
    starts = sorted(set(int(value) for value in np.linspace(voiced_start_sample, last, 4)))
    starts = [value for index, value in enumerate(starts)
              if index == 0 or value - starts[index - 1] >= int(.06 * RATE)]
    candidates, windows = [], []
    for offset in starts:
        start_at = earliest_output_at - .1
        end_at = fixture_end_at + POST_INPUT_SECONDS + 4
        if handoff_at is not None:
            handoff = handoff_at(offset)
            start_at = max(start_at, handoff + acoustic_delay_range[0])
            end_at = min(end_at, handoff + acoustic_delay_range[1] + width / RATE)
        search_start = max(0, int((start_at - capture_start) * RATE))
        search_end = min(len(captured), int((end_at - capture_start) * RATE))
        detection = detect_probe(captured[search_start:search_end], reference[offset:offset + width])
        window = {"reference_offset_ms": round(offset * 1000 / RATE, 2), **detection}
        if detection["status"] == "detected":
            onset = capture_start + search_start / RATE + detection["offset_seconds"]
            onset -= (offset - voiced_start_sample) / RATE
            candidates.append(onset)
            window["estimated_voiced_onset_at"] = onset
        windows.append(window)
    result = {**unavailable, "windows": windows}
    if len(candidates) < 2:
        return {**result, "reason": "Fewer than two reliable acoustic reply windows"}
    spread = (max(candidates) - min(candidates)) * 1000
    if spread > 40:
        return {**result, "reason": "Acoustic reply windows contradict each other", "window_spread_ms": round(spread, 2)}
    onset = float(np.median(candidates))
    latency = (onset - fixture_end_at) * 1000
    if latency < 0 or onset < earliest_output_at - .04:
        return {**result, "reason": "Reply onset predates the injected endpoint or returned audio"}
    return {**result, "status": "detected", "latency_ms": round(latency, 1),
            "fixture_end_to_audible_ms": round(latency, 1), "audible_onset_at": onset,
            "window_spread_ms": round(spread, 2), "resolution_ms": 4.0,
            "method": "multiple generated-reply spectral windows with reference-offset compensation"}
