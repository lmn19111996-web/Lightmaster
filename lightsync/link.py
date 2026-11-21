"""Watches the connection to WLED from a background thread.

- Pings the ESP every PING_EVERY_S. The median of the last few pings decides the state:
  good / slow / bad / down. A queue building up anywhere on the way shows up here first.
- Asks WLED for /json/info every INFO_EVERY_S: Wi-Fi signal, whether it is in realtime
  mode, and which address its realtime data comes from (catches a second instance).
- Logs once when things get worse and once when they recover, never per sample.
`status` is a plain dict, replaced (never mutated), safe to read from any thread.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque

log = logging.getLogger(__name__)

PING_EVERY_S = 2.0
INFO_EVERY_S = 10.0
WINDOW = 5                 # pings used for the median
GOOD_MS, BAD_MS = 50, 200
_RTT = re.compile(r"[=<]\s*(\d+(?:[.,]\d+)?)\s*ms", re.IGNORECASE)
_RANK = {"unknown": 0, "good": 0, "slow": 1, "bad": 2, "down": 3}


def parse_ping(output: str) -> int | None:
    """Round-trip time in ms from one Windows/Linux ping reply (any locale), or None."""
    m = _RTT.search(output)
    return max(1, round(float(m.group(1).replace(",", ".")))) if m else None


def classify(rtts: list[int | None]) -> tuple[str, int | None, float]:
    """(state, median rtt, loss fraction) for a window of ping results."""
    if not rtts:
        return "unknown", None, 0.0
    ok = [r for r in rtts if r is not None]
    loss = 1 - len(ok) / len(rtts)
    if not ok:
        return "down", None, loss
    med = int(statistics.median(ok))
    if med >= BAD_MS or loss >= 0.5:
        return "bad", med, loss
    if med >= GOOD_MS or loss > 0:
        return "slow", med, loss
    return "good", med, loss


def local_ip_towards(host: str) -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))   # no packet is sent; just picks the route
            return s.getsockname()[0]
    except OSError:
        return ""


class LinkMonitor:
    def __init__(self, host: str):
        self.host = host
        self.status: dict = {"state": "unknown"}
        self._pings: deque = deque(maxlen=WINDOW)
        self._info: dict = {}
        self._worst_logged = "good"
        self._info_warned = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="link", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    # --- thread -------------------------------------------------------------

    def _run(self) -> None:
        next_info = time.monotonic() + 3.0   # let the first frames arrive before checking live mode
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._pings.append(self._ping())
                if t0 >= next_info:
                    next_info = t0 + INFO_EVERY_S
                    self._info = self._read_info()
                self._publish()
            except Exception:
                log.exception("link monitor error")
            self._stop.wait(max(0.0, PING_EVERY_S - (time.monotonic() - t0)))

    def _ping(self) -> int | None:
        if sys.platform == "win32":
            cmd = ["ping", "-n", "1", "-w", "1000", self.host]
            flags = subprocess.CREATE_NO_WINDOW
        else:
            cmd = ["ping", "-c", "1", "-W", "1", self.host]
            flags = 0
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                               timeout=3, creationflags=flags)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return parse_ping(r.stdout)

    def _read_info(self) -> dict:
        try:
            with urllib.request.urlopen(f"http://{self.host}/json/info", timeout=2) as r:
                d = json.load(r)
        except (OSError, ValueError):
            return {}
        wifi, leds = d.get("wifi", {}), d.get("leds", {})
        return {"rssi": wifi.get("rssi"), "live": d.get("live"), "mode": d.get("lm"),
                "source": d.get("lip"), "wled_fps": leds.get("fps"), "version": d.get("ver")}

    def _publish(self) -> None:
        state, med, loss = classify(list(self._pings))
        info = self._info
        me = local_ip_towards(self.host)
        foreign = bool(info.get("live") and info.get("source") and me and info["source"] != me)
        self.status = {"state": state, "rtt_ms": med, "loss": round(loss, 2), **info,
                       "foreign_source": foreign}

        # log transitions only
        if _RANK[state] > _RANK[self._worst_logged]:
            log.warning("WLED link %s: median ping %s ms, %d%% lost", state,
                        med if med is not None else "-", round(loss * 100))
            self._worst_logged = state
        elif state == "good" and self._worst_logged != "good":
            log.info("WLED link recovered: median ping %s ms", med)
            self._worst_logged = "good"

        warn = ""
        if info and info.get("live") is False:
            warn = "WLED is not in realtime mode (check Sync settings / another app?)"
        elif foreign:
            warn = f"WLED gets realtime data from {info['source']}, not this PC ({me})"
        if warn and warn != self._info_warned:
            log.warning(warn)
        self._info_warned = warn
