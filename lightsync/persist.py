"""Saves the live state to state.json so a restart keeps the look.

A background thread checks once per second and writes only when something changed
(atomically: temp file + rename, so a crash mid-write never corrupts the file).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from .control import Controller

log = logging.getLogger(__name__)


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable %s: %s", path.name, e)
        return {}


class StateStore:
    def __init__(self, path: Path, ctl: Controller, interval_s: float = 1.0):
        self.path = path
        self.ctl = ctl
        self.interval_s = interval_s
        self.device_latency_ms: dict[str, int] = {}   # replaced (never mutated) by the render thread
        self._keys = [k for k, p in ctl.params.items() if p.persist]
        self._last = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="persist", daemon=True)

    def start(self) -> None:
        self._last = self._serialize()   # don't rewrite right away
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        self.flush()

    def flush(self) -> None:
        s = self._serialize()
        if s == self._last:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(s, encoding="utf-8")
            os.replace(tmp, self.path)
            self._last = s
        except OSError as e:
            log.warning("could not save %s: %s", self.path.name, e)

    def _serialize(self) -> str:
        snap = self.ctl.snapshot()
        data = {"params": {k: snap[k] for k in self._keys},
                "device_latency_ms": dict(self.device_latency_ms)}
        return json.dumps(data, indent=1, sort_keys=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self.flush()
