"""WLED "UDP realtime" sender (DRGB / DNRGB), default port 21324.

Why not DDP: measured on this setup (WLED 0.15.4, ESP32), DDP packets pile up inside
WLED. Pings rose to ~300 ms with heavy loss even at 30 fps, and changes showed up on the
strip about a second late. The same packet rate over UDP realtime kept pings at ~8 ms.

Packets:
    DRGB   [2, timeout, r, g, b, r, g, b, ...]                  up to 490 pixels
    DNRGB  [4, timeout, start_hi, start_lo, r, g, b, ...]       up to 489 pixels per packet
`timeout` = seconds without packets before WLED returns to its normal mode (the failsafe).
"""
from __future__ import annotations

import logging
import socket
import time

import numpy as np

log = logging.getLogger(__name__)

UDP_PORT = 21324
_DRGB, _DNRGB = 2, 4
_DRGB_MAX, _DNRGB_MAX = 490, 489


class WledUdpSender:
    def __init__(self, host: str, port: int = UDP_PORT, timeout_s: int = 2):
        try:
            ip = socket.gethostbyname(host)  # resolve once; never DNS in the hot path
        except OSError:
            log.warning("could not resolve %s, sending to it unresolved", host)
            ip = host
        self.addr = (ip, port)
        self.timeout_s = min(max(int(timeout_s), 1), 254)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self.frames_sent = 0
        self.send_errors = 0
        self._last_err_log = 0.0

    def packets(self, rgb: np.ndarray, timeout_s: int | None = None) -> list[bytes]:
        if rgb.dtype != np.uint8:
            raise TypeError(f"expected uint8 frame, got {rgb.dtype}")
        t = self.timeout_s if timeout_s is None else timeout_s
        data = np.ascontiguousarray(rgb).reshape(-1, 3)
        n = len(data)
        if n <= _DRGB_MAX:
            return [bytes([_DRGB, t]) + data.tobytes()]
        return [bytes([_DNRGB, t, start >> 8, start & 0xFF]) + data[start:start + _DNRGB_MAX].tobytes()
                for start in range(0, n, _DNRGB_MAX)]

    def send(self, rgb: np.ndarray, timeout_s: int | None = None) -> bool:
        """Send one frame. `rgb` is a uint8 array of shape (N, 3). Returns False on error."""
        for pkt in self.packets(rgb, timeout_s):
            try:
                self._sock.sendto(pkt, self.addr)
            except OSError as e:  # includes BlockingIOError when the send buffer is full
                self._note_error(e)
                return False
        self.frames_sent += 1
        return True

    def black(self, pixels: int, repeats: int = 3) -> None:
        """Send a few black frames with a 1 s timeout, so WLED returns to normal quickly."""
        frame = np.zeros((pixels, 3), np.uint8)
        for _ in range(repeats):
            self.send(frame, timeout_s=1)
            time.sleep(0.02)

    def close(self) -> None:
        self._sock.close()

    def _note_error(self, e: OSError) -> None:
        self.send_errors += 1
        now = time.monotonic()
        if now - self._last_err_log > 5.0:  # throttle: at most one log line per 5 s
            log.warning("UDP send failed (%d errors so far): %s", self.send_errors, e)
            self._last_err_log = now
