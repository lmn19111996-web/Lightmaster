"""Control core: the single source of truth for everything a user can change.

- Inputs (web UI, hotkeys, later Home Assistant) call `submit()` from any thread.
  Commands are validated right away, so callers get an error immediately.
- The render loop calls `apply_pending()` once per frame. It is the only writer.
- Other threads read `snapshot()` (an immutable copy, swapped atomically) and
  compare `version` to see whether anything changed.

Commands (plain dicts, the same shape the web UI sends as JSON):
    {"op": "set",    "key": "intensity", "value": 0.8}
    {"op": "nudge",  "key": "latency_ms", "delta": 10}     numbers only, clamped
    {"op": "toggle", "key": "blackout"}                    bools only
    {"op": "cycle",  "key": "color", "dir": 1}             choices, or colors through the palette
"""
from __future__ import annotations

import logging
import math
import queue
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

log = logging.getLogger(__name__)

KINDS = ("float", "int", "bool", "choice", "color")


class CommandError(ValueError):
    pass


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    kind: str                       # one of KINDS
    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()
    group: str = "main"             # UI section
    unit: str = ""
    persist: bool = True            # saved to state.json and restored on start
    when: tuple[str, Any] | None = None   # UI shows this only when param when[0] == when[1]

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"{self.key}: unknown kind {self.kind!r}")
        object.__setattr__(self, "default", self.coerce(self.default))

    def coerce(self, v: Any) -> Any:
        """Validate and normalize a value; numbers are clamped to [min, max]."""
        k = self.kind
        if k == "bool":
            if isinstance(v, bool):
                return v
            raise CommandError(f"{self.key}: expected true or false")
        if k in ("float", "int"):
            if not _is_number(v):
                raise CommandError(f"{self.key}: expected a number")
            v = float(v)
            if self.min is not None:
                v = max(v, self.min)
            if self.max is not None:
                v = min(v, self.max)
            return int(round(v)) if k == "int" else v
        if k == "choice":
            if v not in self.choices:
                raise CommandError(f"{self.key}: expected one of {list(self.choices)}")
            return v
        # color: linear RGB, each 0..1
        if isinstance(v, (list, tuple)) and len(v) == 3 and all(_is_number(c) for c in v):
            return tuple(min(max(float(c), 0.0), 1.0) for c in v)
        raise CommandError(f"{self.key}: expected [r, g, b] with values 0..1")

    def to_json(self) -> dict:
        d = {"key": self.key, "label": self.label, "kind": self.kind,
             "default": self.default, "group": self.group}
        for name in ("min", "max", "step", "unit"):
            if getattr(self, name) not in (None, ""):
                d[name] = getattr(self, name)
        if self.choices:
            d["choices"] = list(self.choices)
        if self.when:
            d["when"] = {"key": self.when[0], "value": self.when[1]}
        return d


class Controller:
    def __init__(self, params: Sequence[Param], palette: Mapping[str, Sequence[float]] | None = None,
                 initial: Mapping[str, Any] | None = None):
        self.params = {p.key: p for p in params}
        if len(self.params) != len(params):
            raise ValueError("duplicate parameter keys")
        self.palette = {name: tuple(float(c) for c in rgb) for name, rgb in (palette or {}).items()}
        self._values = {p.key: p.default for p in params}
        for k, v in (initial or {}).items():  # e.g. restored state.json; ignore stale or bad entries
            if k in self.params:
                try:
                    self._values[k] = self.params[k].coerce(v)
                except CommandError as e:
                    log.warning("ignoring saved value: %s", e)
        self._snapshot: Mapping[str, Any] = MappingProxyType(dict(self._values))
        self.version = 0
        self._queue: queue.SimpleQueue = queue.SimpleQueue()

    # --- any thread ---------------------------------------------------------

    def submit(self, cmd: Mapping[str, Any]) -> None:
        self._queue.put(self._validate(cmd))

    def snapshot(self) -> Mapping[str, Any]:
        return self._snapshot

    def schema(self) -> dict:
        return {"params": [p.to_json() for p in self.params.values()],
                "palette": {k: list(v) for k, v in self.palette.items()}}

    # --- render thread only -------------------------------------------------

    def apply_pending(self) -> bool:
        changed = False
        while True:
            try:
                op, key, arg = self._queue.get_nowait()
            except queue.Empty:
                break
            p, cur = self.params[key], self._values[key]
            if op == "set":
                new = arg
            elif op == "nudge":
                new = p.coerce(cur + arg)
            elif op == "toggle":
                new = not cur
            else:
                new = self._cycle(p, cur, arg)
            if new != cur:
                self._values[key] = new
                changed = True
        if changed:
            self._snapshot = MappingProxyType(dict(self._values))
            self.version += 1
        return changed

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    # --- internals ----------------------------------------------------------

    def _validate(self, cmd: Mapping[str, Any]) -> tuple[str, str, Any]:
        if not isinstance(cmd, Mapping):
            raise CommandError("command must be an object")
        op, key = cmd.get("op"), cmd.get("key")
        p = self.params.get(key)
        if p is None:
            raise CommandError(f"unknown key {key!r}")
        if op == "set":
            return op, key, p.coerce(cmd.get("value"))
        if op == "nudge":
            if p.kind not in ("float", "int"):
                raise CommandError(f"{key}: nudge needs a number parameter")
            if not _is_number(cmd.get("delta")):
                raise CommandError(f"{key}: delta must be a number")
            return op, key, float(cmd["delta"])
        if op == "toggle":
            if p.kind != "bool":
                raise CommandError(f"{key}: toggle needs a bool parameter")
            return op, key, None
        if op == "cycle":
            if p.kind not in ("choice", "color") or (p.kind == "color" and not self.palette):
                raise CommandError(f"{key}: cannot cycle this parameter")
            d = cmd.get("dir", 1)
            if d not in (1, -1):
                raise CommandError(f"{key}: dir must be 1 or -1")
            return op, key, d
        raise CommandError(f"unknown op {op!r}")

    def _cycle(self, p: Param, cur: Any, d: int) -> Any:
        if p.kind == "choice":
            options = list(p.choices)
            return options[(options.index(cur) + d) % len(options)]
        colors = list(self.palette.values())
        i = next((n for n, c in enumerate(colors)
                  if all(abs(a - b) < 1e-6 for a, b in zip(c, cur))), None)
        if i is None:  # custom color: start at the first (or last) palette entry
            return colors[0] if d == 1 else colors[-1]
        return colors[(i + d) % len(colors)]
