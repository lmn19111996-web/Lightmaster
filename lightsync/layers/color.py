"""Color layer -> (N, 3) linear RGB.

Phase 2 (temporary): solid base color whose brightness pulses with the bass.
Phase 4 adds state behavior (breathe / swells / hue drift).

Two pulse styles (live parameter `pulse_mode`):

  smooth  bass level ─> × sensitivity ─> envelope (10 ms attack, `decay` release) ─> ^ contrast
  beat    two states. A hit fires when the bass is above `threshold` AND jumps clearly
          above its recent average (so a sustained bassline fires once, not continuously).
          On a hit the pulse goes to 1, holds for `hold`, then drops with `fade`.
          After a hit the detector must settle (re-arm) before it can fire again.

Brightness = level × (1 − depth + depth × pulse)
"""
from __future__ import annotations

import math

import numpy as np

from ..analyzer import Features

ATTACK_MS = 10.0
BASELINE_MS = 250.0     # "recent average" of the bass level for beat detection
RISE_MIN = 0.10         # a hit must be at least this far above the recent average
REARM_RISE = 0.03       # re-arm once the level is back near its average (or below threshold)
REFRACTORY_S = 0.10     # never more than 10 hits per second


def _coef(ms: float, fps: float) -> float:
    return 1.0 - math.exp(-1000.0 / (max(ms, 1.0) * fps))


class Pulse:
    def __init__(self, fps: float):
        self.fps = fps
        self.value = 0.0      # envelope
        self.out = 0.0        # final pulse 0..1
        self.hit = False      # True on the frame a beat fired
        self.hits = 0
        self._baseline = 0.0
        self._armed = True
        self._hold_left = 0.0
        self._since_hit = 1e9

    def smooth(self, level: float, sensitivity: float, decay_ms: float, contrast: float) -> float:
        self._track_baseline(level)
        self.hit = False
        x = min(max(level * sensitivity, 0.0), 1.0)
        ms = ATTACK_MS if x > self.value else decay_ms
        self.value += (x - self.value) * _coef(ms, self.fps)
        self.out = self.value ** contrast
        return self.out

    def beat(self, level: float, threshold: float, hold_ms: float, decay_ms: float) -> float:
        dt = 1.0 / self.fps
        self._since_hit += dt
        rise = level - self._baseline
        self._track_baseline(level)

        self.hit = False
        if self._armed and level >= threshold and rise >= RISE_MIN and self._since_hit >= REFRACTORY_S:
            self.hit = True
            self.hits += 1
            self._armed = False
            self._since_hit = 0.0
            self._hold_left = hold_ms / 1000.0
        elif not self._armed and (level < threshold or rise < REARM_RISE):
            self._armed = True

        if self.hit or self._hold_left > 0:
            self._hold_left -= dt
            self.value = 1.0
        else:
            self.value -= self.value * _coef(decay_ms, self.fps)
        self.out = self.value
        return self.out

    def _track_baseline(self, level: float) -> None:
        self._baseline += (level - self._baseline) * _coef(BASELINE_MS, self.fps)


class ColorLayer:
    def __init__(self, pixels: int, fps: float):
        self.pixels = pixels
        self.pulse = Pulse(fps)

    def render(self, color, level: float, f: Features, *, mode: str = "smooth", depth: float,
               decay_ms: float, sensitivity: float = 1.0, contrast: float = 1.0,
               threshold: float = 0.5, hold_ms: float = 80.0, fade_ms: float = 40.0) -> np.ndarray:
        if mode == "beat":
            p = self.pulse.beat(f.bass, threshold, hold_ms, fade_ms)
        else:
            p = self.pulse.smooth(f.bass, sensitivity, decay_ms, contrast)
        k = level * (1.0 - depth + depth * p)
        return np.tile(np.asarray(color, np.float32) * k, (self.pixels, 1))
