"""Audio analysis, run by the render loop once per frame.

Phase 2 (temporary): auto-gained bass level. The envelope/pulse shaping lives in the
color layer, so its parameters can be changed live from the UI.
Phase 4 adds onsets/beats and the chill/medium/peak energy state.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Features:
    bass: float = 0.0       # 0..1 auto-gained bass level (no smoothing)
    silent: bool = True


class BassAnalyzer:
    def __init__(self, fps: float, n_fft: int = 2048, band=(40.0, 150.0),
                 gain_hold_s: float = 30.0, noise_floor: float = 1e-3):
        self.n_fft = n_fft
        self.band = band
        self.gain_decay = np.exp(-1.0 / (gain_hold_s * fps))  # slow peak tracker for auto-gain
        self.noise_floor = noise_floor
        self._window = np.hanning(n_fft).astype(np.float32)
        self._mask_rate = None
        self._mask = None
        self._ref = noise_floor

    def update(self, samples: np.ndarray, rate: int) -> Features:
        if self._mask_rate != rate:
            freqs = np.fft.rfftfreq(self.n_fft, 1.0 / rate)
            self._mask = (freqs >= self.band[0]) & (freqs <= self.band[1])
            self._mask_rate = rate
        spec = np.abs(np.fft.rfft(samples[-self.n_fft:] * self._window))
        level = float(np.sqrt(np.mean(spec[self._mask] ** 2))) / self.n_fft

        # auto-gain: follow the loudest recent bass, decay slowly, never below the noise floor
        self._ref = max(level, self._ref * self.gain_decay, self.noise_floor)
        silent = level < self.noise_floor * 0.5
        return Features(bass=0.0 if silent else min(level / self._ref, 1.0), silent=silent)


class DelayLine:
    """Delays per-frame values by a variable number of frames (for Bluetooth latency)."""

    def __init__(self, max_frames: int):
        self._buf: deque = deque(maxlen=max_frames + 1)

    def push(self, item, delay_frames: int):
        self._buf.append(item)
        d = min(max(delay_frames, 0), len(self._buf) - 1)
        return self._buf[-1 - d]
