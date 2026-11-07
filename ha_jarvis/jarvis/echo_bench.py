"""Offline far-end pilot suppression; no live routing or calibration acceptance."""
import math

import numpy as np

from .aec import FRAME_SAMPLES, SAMPLE_RATE, HostAEC3

SCOPE = 'offline far-end-only quiet-room processing; no near-end speech/drift acceptance'


def _bands(pcm):
    window = np.hanning(512)
    frames = np.lib.stride_tricks.sliding_window_view(pcm.astype(float) / 32768, 512)[::160]
    power = abs(np.fft.rfft(frames * window, axis=1)) ** 2 * 2 / (512 * sum(window ** 2))
    return np.stack([power[:, round(hz * 512 / SAMPLE_RATE) - 1:
        round(hz * 512 / SAMPLE_RATE) + 2].sum(axis=1) for hz in (937, 1579)], axis=1)


def measure_echo_reduction(captured_int16, capture_start, rendered_int16, render_start,
                           measured_delay_ms, *, precleaned=None):
    result = {'status': 'unavailable', 'reason': None, 'scope': SCOPE,
              'pilot_band_reduction_db': None, 'delay_assumed_ms': None,
              'raw_pilot_band_dbfs': None, 'processed_pilot_band_dbfs': None,
              'noise_floor_band_dbfs': None, 'noise_floor_limited': None,
              'max_processor_call_ms': None}
    if precleaned is not None:
        result['scope'] = 'DMA-confirmed live far-end processing; no near-end speech acceptance'

    def unavailable(reason):
        return {**result, 'reason': reason}
    for pcm in (captured_int16, rendered_int16):
        if not isinstance(pcm, np.ndarray) or pcm.ndim != 1 or pcm.dtype != np.dtype('int16'):
            return unavailable('Capture and reference must be mono int16 arrays')
        if len(pcm) > 120 * SAMPLE_RATE:
            return unavailable('Offline echo bench is limited to 120 seconds per input')
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
               for v in (capture_start, render_start, measured_delay_ms)) or not 0 <= measured_delay_ms <= 15000:
        return unavailable('Finite timestamps and an assumed delay from 0 to 15000 ms are required')
    result['delay_assumed_ms'] = round(measured_delay_ms, 3)
    n = len(captured_int16) // FRAME_SAMPLES * FRAME_SAMPLES
    if len(rendered_int16) < 4 * SAMPLE_RATE or n < 3 * SAMPLE_RATE:
        return unavailable('Capture or reference is too short for a two-second pilot analysis')
    if np.any(rendered_int16[-SAMPLE_RATE:]):
        return unavailable('Known reference must end with one second of digital silence')
    offset = round((render_start + measured_delay_ms / 1000 - capture_start) * SAMPLE_RATE)
    start, end = offset + len(rendered_int16) - 3 * SAMPLE_RATE, offset + len(rendered_int16) - SAMPLE_RATE
    quiet_end = min(n, offset - SAMPLE_RATE // 10)
    if start < 0 or end > n or quiet_end < SAMPLE_RATE // 4:
        return unavailable('Capture lacks the full final pilot or a quiet pre-roll baseline')
    reference_bands = _bands(rendered_int16[-3 * SAMPLE_RATE:-SAMPLE_RATE])
    if np.any(np.median(reference_bands, axis=0) < 1e-7):
        return unavailable('Known final reference interval does not contain both pilot bands')
    if np.mean(abs(captured_int16.astype(float)) >= 32760) > .005:
        return unavailable('Capture is clipped; suppression measurement is unavailable')
    aligned = np.zeros(n, dtype='<i2')
    dest, source = max(0, offset), max(0, -offset)
    count = min(n - dest, len(rendered_int16) - source)
    if count > 0:
        aligned[dest:dest + count] = rendered_int16[source:source + count]
    raw = _bands(captured_int16[start:end])
    quiet_start = max(0, quiet_end - 2 * SAMPLE_RATE)
    # An entire PCM LSB squared overstates the floor of a three-bin band.
    # Bound each band by its share of white 16-bit quantization noise.
    band_quantization_floor = (2 * 3 / 512) / (12 * 32768 ** 2)
    raw_floor = np.maximum(np.percentile(_bands(captured_int16[quiet_start:quiet_end]), 95, axis=0),
                           band_quantization_floor)
    raw_level = np.median(raw, axis=0)
    result['raw_pilot_snr_db'] = round(10 * math.log10(sum(raw_level) / sum(raw_floor)), 2)
    db = lambda value: round(10 * math.log10(max(float(value), 1e-15)), 2)
    result.update(raw_pilot_band_dbfs=db(sum(raw_level)), noise_floor_band_dbfs=db(sum(raw_floor)))
    if result['raw_pilot_snr_db'] < 8 or np.any(raw_level < raw_floor * 4):
        return unavailable('Raw acoustic pilot is too weak relative to quiet pre-roll noise')
    if precleaned is None:
        try:
            processor = HostAEC3(reference_delay_ms=0, stream_delay_ms=0)
        except (ImportError, RuntimeError) as exc:
            return unavailable(f'Native AEC3 backend is unavailable ({type(exc).__name__})')
        processed = np.empty(n, dtype='<i2')
        for index in range(0, n, FRAME_SAMPLES):
            processed[index:index + FRAME_SAMPLES] = np.frombuffer(processor.process(
                captured_int16[index:index + FRAME_SAMPLES].astype('<i2', copy=False).tobytes(),
                aligned[index:index + FRAME_SAMPLES].tobytes()), dtype='<i2')
        result['max_processor_call_ms'] = round(processor.max_processing_ms, 3)
    elif precleaned.ndim != 1 or precleaned.dtype != np.dtype('int16') or len(precleaned) != n:
        return unavailable('Live echo output does not cover the complete aligned capture')
    else:
        processed = precleaned
    level = float(sum(np.median(_bands(processed[start:end]), axis=0)))
    floor = max(float(sum(raw_floor)), float(sum(np.percentile(_bands(processed[quiet_start:quiet_end]), 95, axis=0))))
    limited = level <= floor * 2
    result.update(status='measured', reason='Reduction is bounded by the measured noise floor' if limited else 'Pilot-band power comparison only; not full-band ERLE',
        processed_pilot_band_dbfs=db(level), noise_floor_band_dbfs=db(floor), noise_floor_limited=limited,
        pilot_band_reduction_db=round(10 * math.log10(sum(raw_level) / max(level, floor)), 2),
        ignored_tail_samples=len(captured_int16) - n)
    return result
