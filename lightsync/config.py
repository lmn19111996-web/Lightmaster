"""Loads and validates config.yaml. Fails at startup with a clear message, never mid-show."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .compositor import OVERFLOW_MODES
from .output import DEFAULT_PORTS, PROTOCOLS


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SpotConfig:
    width_px: int = 2
    gap_px: int = 5
    max: float = 1.0
    decay_ms: float = 300.0
    duck: float = 0.4
    white_tint: tuple = (1.0, 0.82, 0.67)
    overflow: str = "clip"


@dataclass(frozen=True)
class PulseConfig:
    mode: str = "beat"          # beat = two states (off / hit), smooth = follows the bass level
    threshold: float = 0.5      # beat: how strong a hit must be (0..1 of the auto-gained bass)
    hold_ms: int = 80           # beat: how long a hit stays fully on
    fade_ms: int = 25           # beat: how fast it drops back after the hold
    depth: float = 0.9          # share of the brightness that pulses (0 = steady, 1 = dark between hits)
    sensitivity: float = 1.3    # >1 reaches full brightness on softer hits
    contrast: float = 2.0       # >1 keeps quiet parts down and makes hits pop
    decay_ms: int = 150         # how fast the pulse falls after a hit


@dataclass(frozen=True)
class WebConfig:
    port: int = 8080
    lan: bool = False          # False: only this PC (127.0.0.1); True: whole network, PIN required
    pin: str = ""
    open_browser: bool = True


@dataclass(frozen=True)
class Config:
    host: str
    protocol: str
    port: int
    udp_timeout_s: int
    pixels: int
    fps: int
    mode: str
    color: tuple
    color_max: float
    cozy_color_max: float
    spot: SpotConfig
    pulse: PulseConfig
    latency_ms: int
    device_latency_ms: dict
    colors: dict
    web: WebConfig = field(default_factory=WebConfig)


def _rgb(v, where: str) -> tuple:
    if (not isinstance(v, (list, tuple)) or len(v) != 3
            or not all(isinstance(c, (int, float)) and 0 <= c <= 1 for c in v)):
        raise ConfigError(f"{where}: expected [r, g, b] with values 0..1, got {v!r}")
    return tuple(float(c) for c in v)


def _num(d: dict, key: str, lo: float, hi: float, where: str, default=None, kind=float):
    v = d.get(key, default)
    if v is None:
        raise ConfigError(f"{where}.{key} is missing")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ConfigError(f"{where}.{key}: expected a number in {lo}..{hi}, got {v!r}")
    return kind(v)


def load(path: str | Path) -> Config:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e

    wled = raw.get("wled") or {}
    strip = raw.get("strip") or {}
    spot = raw.get("spot") or {}
    audio = raw.get("audio") or {}
    pulse = raw.get("pulse") or {}
    web = raw.get("web") or {}

    colors = {str(k): _rgb(v, f"colors.{k}") for k, v in (raw.get("colors") or {}).items()}
    if not colors:
        raise ConfigError("colors: define at least one named color")
    color = raw.get("color")
    if isinstance(color, str):
        if color not in colors:
            raise ConfigError(f"color: unknown name {color!r} (known: {', '.join(colors)})")
        color = colors[color]
    else:
        color = _rgb(color, "color")

    mode = raw.get("mode", "performance")
    if mode not in ("cozy", "performance"):
        raise ConfigError(f"mode: expected cozy or performance, got {mode!r}")
    overflow = spot.get("overflow", "clip")
    if overflow not in OVERFLOW_MODES:
        raise ConfigError(f"spot.overflow: expected one of {OVERFLOW_MODES}, got {overflow!r}")
    host = wled.get("host")
    if not host or not isinstance(host, str):
        raise ConfigError("wled.host is missing")
    pulse_mode = pulse.get("mode", "beat")
    if pulse_mode not in ("beat", "smooth"):
        raise ConfigError(f"pulse.mode: expected beat or smooth, got {pulse_mode!r}")
    protocol = wled.get("protocol", "udp")
    if protocol not in PROTOCOLS:
        raise ConfigError(f"wled.protocol: expected one of {PROTOCOLS}, got {protocol!r}")
    pin = str(web.get("pin", "") or "")
    lan = bool(web.get("lan", False))
    if lan and not pin:
        raise ConfigError("web.lan is true, so web.pin must be set")

    dev_lat = audio.get("device_latency_ms") or {}
    for name, ms in dev_lat.items():
        _num({"v": ms}, "v", 0, 500, f"audio.device_latency_ms.{name}")

    return Config(
        host=host,
        protocol=protocol,
        port=_num(wled, "port", 1, 65535, "wled", DEFAULT_PORTS[protocol], int),
        udp_timeout_s=_num(wled, "udp_timeout_s", 1, 254, "wled", 2, int),
        pixels=_num(strip, "pixels", 1, 10000, "strip", None, int),
        fps=_num(raw, "fps", 10, 120, "config", 60, int),
        mode=mode,
        color=color,
        color_max=_num(raw, "color_max", 0, 1, "config", 0.2),
        cozy_color_max=_num(raw, "cozy_color_max", 0, 1, "config", 0.04),
        spot=SpotConfig(
            width_px=_num(spot, "width_px", 1, 1000, "spot", 2, int),
            gap_px=_num(spot, "gap_px", 0, 1000, "spot", 5, int),
            max=_num(spot, "max", 0, 1, "spot", 1.0),
            decay_ms=_num(spot, "decay_ms", 10, 5000, "spot", 300),
            duck=_num(spot, "duck", 0, 1, "spot", 0.4),
            white_tint=_rgb(spot.get("white_tint", [1.0, 0.82, 0.67]), "spot.white_tint"),
            overflow=overflow,
        ),
        pulse=PulseConfig(
            mode=pulse_mode,
            threshold=_num(pulse, "threshold", 0.05, 1, "pulse", 0.5),
            hold_ms=_num(pulse, "hold_ms", 0, 500, "pulse", 80, int),
            fade_ms=_num(pulse, "fade_ms", 10, 1000, "pulse", 25, int),
            depth=_num(pulse, "depth", 0, 1, "pulse", 0.9),
            sensitivity=_num(pulse, "sensitivity", 0.25, 4, "pulse", 1.3),
            contrast=_num(pulse, "contrast", 0.5, 4, "pulse", 2.0),
            decay_ms=_num(pulse, "decay_ms", 20, 1000, "pulse", 150, int),
        ),
        latency_ms=_num(audio, "latency_ms", 0, 500, "audio", 0, int),
        device_latency_ms={str(k): int(v) for k, v in dev_lat.items()},
        colors=colors,
        web=WebConfig(port=_num(web, "port", 1, 65535, "web", 8080, int), lan=lan, pin=pin,
                      open_browser=bool(web.get("open_browser", True))),
    )


def latency_for_device(cfg: Config, device_name: str) -> int:
    """Per-device latency if a configured name is part of the device name, else the default."""
    for needle, ms in cfg.device_latency_ms.items():
        if needle.lower() in device_name.lower():
            return ms
    return cfg.latency_ms
