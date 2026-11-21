"""Compositor: mixes the layers in linear light, then encodes to 8-bit.

    out = C * (1 - duck * w) + w * W_tint        (layers stay independent; ducking happens here)
    overflow: "clip"      -> values above 1 burn out toward white
              "normalize" -> divide by the brightest channel, so a flash keeps the song tint
    encode:   v = 255 * L ** (1/2.2), with temporal dithering for smooth dim fades
"""
from __future__ import annotations

import numpy as np

OVERFLOW_MODES = ("clip", "normalize")


def mix(C: np.ndarray, w: np.ndarray, tint, duck: float, overflow: str = "clip") -> np.ndarray:
    """C: (N, 3) linear color layer; w: (N,) white layer; tint: RGB of the white. Returns (N, 3) in 0..1."""
    w2 = w[:, None]
    out = C * (1.0 - duck * w2) + w2 * np.asarray(tint, dtype=np.float32)
    if overflow == "normalize":
        out = out / np.maximum(out.max(axis=1, keepdims=True), 1.0)
    elif overflow != "clip":
        raise ValueError(f"unknown overflow mode {overflow!r}")
    return np.clip(out, 0.0, 1.0)


class Encoder:
    """Linear 0..1 -> gamma-encoded uint8 with per-pixel temporal error diffusion."""

    def __init__(self, pixels: int, gamma: float = 2.2, dither: bool = True):
        self.inv_gamma = 1.0 / gamma
        self.dither = dither
        self._residual = np.zeros((pixels, 3), np.float32)

    def encode(self, L: np.ndarray) -> np.ndarray:
        L = np.clip(L, 0.0, 1.0)
        v = 255.0 * np.power(L, self.inv_gamma, dtype=np.float32)
        if not self.dither:
            return (v + 0.5).astype(np.uint8)
        t = v + self._residual
        q = np.clip(np.floor(t + 0.5), 0.0, 255.0)
        self._residual = t - q              # stays within about ±0.5
        self._residual[L <= 0.0] = 0.0      # true black stays black, no ghost glow
        return q.astype(np.uint8)
