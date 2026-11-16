"""Local, render-conditioned evidence for speech over speaker playback."""
from collections import deque
from dataclasses import dataclass

import numpy as np

from .audio import RATE


HOP = RATE // 50
WINDOW = 2 * HOP
_FREQUENCIES = np.fft.rfftfreq(WINDOW, 1 / RATE)
_EDGES = (175, 325, 525, 825, 1225, 1825, 2725, 4025, 5975)
_BANDS = np.array([(_FREQUENCIES >= lo) & (_FREQUENCIES < hi)
                   for lo, hi in zip(_EDGES, _EDGES[1:])], dtype=float)
_TAPER = np.hanning(WINDOW)


def _power(windows):
    spectrum = np.fft.rfft(windows * _TAPER, axis=-1)
    return (np.abs(spectrum) ** 2) @ _BANDS.T


def _blocks(samples):
    return _power(np.lib.stride_tricks.sliding_window_view(samples, WINDOW)[::HOP])


class RenderBands:
    """Power indexed by end sample on the render clock."""

    def __init__(self, mono_pcm):
        self.power = _blocks(np.pad(mono_pcm, (WINDOW, 0)))

    def envelope(self, end_sample, confirmed_samples):
        index = round(end_sample / HOP)
        # Allow timing error and late acoustic energy without reading past
        # the latest device-confirmed portion of the render.
        start = max(0, index - 6)
        end = min(len(self.power), index + 2, confirmed_samples // HOP + 1)
        if end <= start:
            return np.zeros(len(_BANDS))
        power = np.max(self.power[start:end], axis=0)
        # A small broadband term accounts for spill between bands.
        return power + .025 * power.sum()


@dataclass(frozen=True)
class EchoBandProfile:
    noise: np.ndarray
    transfer: np.ndarray

    def third_band_ratio(self, samples, render_power):
        # Require energy unexplained by the acoustic calibration in at
        # least three bands, so a single tone is insufficient.
        bound = 2 * (self.noise + self.transfer * render_power)
        return float(np.partition(_power(samples) / bound, -3)[-3])


def calibrate_echo_bands(captured, capture_start, reference, acoustic_starts):
    """Fit a conservative band-power bound from known playback-only clips."""
    starts = [round((start - capture_start) * RATE) for start in acoustic_starts]
    if not starts or len(reference) < 10 * HOP or min(starts) < 6 * HOP:
        return None
    if max(starts) + len(reference) > len(captured):
        return None
    before = min(starts) - 2 * HOP
    quiet = captured[max(0, before - RATE // 3):before]
    noise = np.maximum(1., np.percentile(_blocks(quiet), 90, axis=0))
    render = RenderBands(reference)
    ends = range(WINDOW, len(reference) + 1, HOP)
    powers = np.array([render.envelope(end, len(reference)) for end in ends])
    total = powers.sum(axis=1)
    if not np.any(total):
        return None
    active = total >= max(1., .02 * np.percentile(total, 95))
    if not np.any(active):
        return None
    microphone = np.concatenate([_blocks(captured[start:start + len(reference)])[active] for start in starts])
    # An inaudible clip or a route that changed during calibration cannot
    # establish a usable speaker-to-microphone transfer.
    if np.percentile(microphone.sum(axis=1), 90) < 4 * noise.sum():
        return None
    transfer = np.maximum(microphone - noise, 0) / np.tile(np.maximum(powers[active], 1.), (len(starts), 1))
    return EchoBandProfile(noise, np.percentile(transfer, 95, axis=0))


class InterruptionEvidence:
    """Check raw broadband novelty alongside AEC-cleaned speech activity."""

    def __init__(self, profile, reference, first_sample):
        self.profile = profile
        self.render = RenderBands(reference)
        self.first_sample = first_sample
        self.samples = np.zeros(RATE, dtype="<i2")
        self.latest = 0
        self.recent_ratios = deque(maxlen=5)

    def offer_capture(self, frame):
        start = frame.first_sample - self.first_sample
        if start != self.latest:
            raise ValueError("Discontinuous microphone history during interruption detection")
        end = start + len(frame.samples)
        self.samples[np.arange(start, end) % len(self.samples)] = frame.samples
        self.latest = end

    def observe(self, capture_end, render_end, confirmed_samples):
        start = capture_end - WINDOW
        if start < max(0, self.latest - len(self.samples)) or capture_end > self.latest:
            self.recent_ratios.clear()
            return None, 0.
        mic = self.samples[np.arange(start, capture_end) % len(self.samples)]
        ratio = self.profile.third_band_ratio(mic, self.render.envelope(render_end, confirmed_samples))
        self.recent_ratios.append(ratio)
        # Raw acoustic evidence can precede the AEC/VAD decision. Keep the
        # second-strongest window long enough for the two signals to agree.
        sustained = sorted(self.recent_ratios)[-2] if len(self.recent_ratios) >= 2 else 0.
        return ratio, sustained
