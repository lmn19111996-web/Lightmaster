"""Terminal preview: draws the strip as one row of true-color blocks (for tuning without the strip)."""
from __future__ import annotations

import os
import sys
import time

import numpy as np


class TerminalPreview:
    def __init__(self, fps: float = 20.0):
        self.period = 1.0 / fps
        self._next = 0.0
        if os.name == "nt":
            os.system("")  # enables ANSI escape codes in the Windows console

    def show(self, rgb: np.ndarray, status: str = "") -> None:
        now = time.monotonic()
        if now < self._next:
            return
        self._next = now + self.period
        row = "".join(f"\x1b[48;2;{r};{g};{b}m " for r, g, b in rgb.tolist())
        sys.stdout.write(f"\r{row}\x1b[0m {status}\x1b[K")
        sys.stdout.flush()
