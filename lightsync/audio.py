"""System audio capture via WASAPI loopback of the default output device.

Runs in its own thread and never blocks the render loop: the audio callback writes
mono float32 samples into a ring buffer, and the render loop copies the newest
samples out whenever it wants.

Device changes: PortAudio only sees a new default device after a full re-init.
So the capture thread re-initializes and reopens
  - when the stream stops or errors (e.g. a Bluetooth speaker disconnects), and
  - after `reopen_after_silence_s` of silence. If you switched the default device,
    the music now plays elsewhere and the old device goes quiet, so the reopen
    picks up the new default. While music plays, nothing is ever reopened.
"""
from __future__ import annotations

import logging
import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio

log = logging.getLogger(__name__)

SILENCE_LEVEL = 1e-4       # peak amplitude below this counts as silence (about -80 dBFS)
STALE_AFTER_S = 0.25       # no samples for this long -> treat as silence


class RingBuffer:
    def __init__(self, size: int):
        self._buf = np.zeros(size, np.float32)
        self._pos = 0
        self._lock = threading.Lock()
        self.last_write = 0.0

    def write(self, x: np.ndarray) -> None:
        size, n = len(self._buf), len(x)
        with self._lock:
            if n >= size:
                self._buf[:] = x[-size:]
                self._pos = 0
            else:
                end = self._pos + n
                if end <= size:
                    self._buf[self._pos:end] = x
                else:
                    k = size - self._pos
                    self._buf[self._pos:] = x[:k]
                    self._buf[:n - k] = x[k:]
                self._pos = end % size
            self.last_write = time.monotonic()

    def latest(self, n: int) -> np.ndarray:
        with self._lock:
            idx = (self._pos - n + np.arange(n)) % len(self._buf)
            return self._buf[idx]   # fancy indexing returns a copy


class AudioCapture:
    def __init__(self, block: int = 512, ring_size: int = 1 << 17, reopen_after_silence_s: float = 3.0):
        self.block = block
        self.reopen_after_silence_s = reopen_after_silence_s
        self.ring = RingBuffer(ring_size)
        self.rate = 48000
        self.device_name = ""
        self.device_changes = 0
        self._channels = 2
        self._last_sound = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="audio", daemon=True)

    # --- public -------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def latest(self, n: int) -> np.ndarray:
        """Newest n mono samples, or zeros if capture is silent/stalled."""
        if time.monotonic() - self.ring.last_write > STALE_AFTER_S:
            return np.zeros(n, np.float32)
        return self.ring.latest(n)

    # --- capture thread -----------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session()
            except Exception:
                log.exception("audio capture failed, retrying in 2 s")
                self._stop.wait(2.0)

    def _session(self) -> None:
        p = pyaudio.PyAudio()
        stream = None
        try:
            dev = self._default_loopback(p)
            name = dev["name"].replace(" [Loopback]", "")
            if name != self.device_name:
                if self.device_name:
                    self.device_changes += 1
                log.info("capturing '%s' (%d Hz, %d ch)", name,
                         int(dev["defaultSampleRate"]), dev["maxInputChannels"])
            self.rate = int(dev["defaultSampleRate"])
            self._channels = max(1, int(dev["maxInputChannels"]))
            stream = p.open(format=pyaudio.paFloat32, channels=self._channels, rate=self.rate,
                            input=True, input_device_index=dev["index"],
                            frames_per_buffer=self.block, stream_callback=self._callback)
            self.device_name = name
            opened = time.monotonic()
            while not self._stop.wait(0.5):
                if not stream.is_active():
                    log.warning("audio stream stopped, reopening")
                    return
                if time.monotonic() - max(self._last_sound, opened) > self.reopen_after_silence_s:
                    return  # silent: reopen to pick up a changed default device
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            p.terminate()

    @staticmethod
    def _default_loopback(p: "pyaudio.PyAudio") -> dict:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        speakers = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if speakers.get("isLoopbackDevice"):
            return speakers
        for lb in p.get_loopback_device_info_generator():
            if speakers["name"] in lb["name"]:
                return lb
        raise RuntimeError(f"no loopback device for '{speakers['name']}'")

    def _callback(self, in_data, frame_count, time_info, status):
        x = np.frombuffer(in_data, np.float32)
        if self._channels > 1:
            x = x.reshape(-1, self._channels).mean(axis=1)
        self.ring.write(x)
        if x.size and float(np.abs(x).max()) > SILENCE_LEVEL:
            self._last_sound = time.monotonic()
        return None, pyaudio.paContinue
