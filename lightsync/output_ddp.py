"""DDP (Distributed Display Protocol) sender for WLED.

Fire-and-forget UDP. It never blocks and never raises on network errors, so a
WLED reboot or a Wi-Fi hiccup cannot stall or crash the render loop.

Packet = 10-byte header + RGB data:
    byte 0    flags: 0x40 (version 1) | 0x01 (PUSH, on the last packet of a frame)
    byte 1    sequence number 1..15 (0 means "unused")
    byte 2    data type: 0x0B = RGB, 8 bit per channel
    byte 3    destination id: 0x01 = default output device
    byte 4-7  data offset in bytes (big endian)
    byte 8-9  data length in bytes (big endian)
"""
from __future__ import annotations

import logging
import socket
import struct
import time

import numpy as np

log = logging.getLogger(__name__)

DDP_PORT = 4048
MAX_DATA = 1440  # 480 RGB pixels per packet, stays well below a typical MTU

_HEADER = struct.Struct("!BBBBIH")
_FLAGS_V1 = 0x40
_FLAG_PUSH = 0x01
_TYPE_RGB8 = 0x0B
_DEST_DISPLAY = 0x01


class DDPSender:
    def __init__(self, host: str, port: int = DDP_PORT):
        try:
            ip = socket.gethostbyname(host)  # resolve once; never DNS in the hot path
        except OSError:
            log.warning("could not resolve %s, sending to it unresolved", host)
            ip = host
        self.addr = (ip, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._seq = 0
        self.frames_sent = 0
        self.send_errors = 0
        self._last_err_log = 0.0

    def send(self, rgb: np.ndarray) -> bool:
        """Send one frame. `rgb` is a uint8 array of shape (N, 3). Returns False on error."""
        if rgb.dtype != np.uint8:
            raise TypeError(f"expected uint8 frame, got {rgb.dtype}")
        data = np.ascontiguousarray(rgb).tobytes()
        if not data:
            return True
        if len(data) % 3:
            raise ValueError("frame size must be a multiple of 3 bytes (RGB)")

        self._seq = self._seq % 15 + 1
        for off in range(0, len(data), MAX_DATA):
            chunk = data[off:off + MAX_DATA]
            last = off + MAX_DATA >= len(data)
            flags = _FLAGS_V1 | (_FLAG_PUSH if last else 0)
            header = _HEADER.pack(flags, self._seq, _TYPE_RGB8, _DEST_DISPLAY, off, len(chunk))
            try:
                self._sock.sendto(header + chunk, self.addr)
            except OSError as e:  # includes BlockingIOError when the send buffer is full
                self._note_error(e)
                return False
        self.frames_sent += 1
        return True

    def black(self, pixels: int, repeats: int = 3) -> None:
        """Send a few black frames (UDP can drop one)."""
        frame = np.zeros((pixels, 3), np.uint8)
        for _ in range(repeats):
            self.send(frame)
            time.sleep(0.02)

    def close(self) -> None:
        self._sock.close()

    def _note_error(self, e: OSError) -> None:
        self.send_errors += 1
        now = time.monotonic()
        if now - self._last_err_log > 5.0:  # throttle: at most one log line per 5 s
            log.warning("DDP send failed (%d errors so far): %s", self.send_errors, e)
            self._last_err_log = now
