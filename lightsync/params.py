"""Core parameters: everything the UI and hotkeys can change. Defaults come from config.yaml."""
from __future__ import annotations

from .config import Config
from .control import Param


def core_params(cfg: Config) -> list[Param]:
    pc = cfg.pulse
    return [
        Param("mode", "Mode", "choice", cfg.mode, choices=("cozy", "performance"), group="main"),
        Param("color", "Color", "color", cfg.color, group="main"),
        Param("intensity", "Intensity", "float", 1.0, min=0, max=1, step=0.01, group="main"),
        Param("blackout", "Blackout", "bool", False, group="main", persist=False),

        Param("pulse_mode", "Style", "choice", pc.mode, choices=("beat", "smooth"), group="pulse"),
        Param("pulse_depth", "Depth", "float", pc.depth, min=0, max=1, step=0.01, group="pulse"),
        Param("pulse_threshold", "Threshold", "float", pc.threshold, min=0.05, max=1, step=0.01,
              group="pulse", when=("pulse_mode", "beat")),
        Param("pulse_hold_ms", "Hold", "int", pc.hold_ms, min=0, max=500, step=10, unit="ms",
              group="pulse", when=("pulse_mode", "beat")),
        Param("pulse_sensitivity", "Sensitivity", "float", pc.sensitivity,
              min=0.25, max=4, step=0.05, unit="×", group="pulse", when=("pulse_mode", "smooth")),
        Param("pulse_contrast", "Contrast", "float", pc.contrast, min=0.5, max=4, step=0.1,
              group="pulse", when=("pulse_mode", "smooth")),
        Param("pulse_fade_ms", "Fade", "int", pc.fade_ms, min=10, max=1000, step=10,
              unit="ms", group="pulse", when=("pulse_mode", "beat")),
        Param("pulse_decay_ms", "Decay", "int", pc.decay_ms, min=20, max=1000, step=10,
              unit="ms", group="pulse", when=("pulse_mode", "smooth")),

        Param("color_max", "Color level (performance)", "float", cfg.color_max,
              min=0, max=1, step=0.01, group="levels"),
        Param("cozy_color_max", "Color level (cozy)", "float", cfg.cozy_color_max,
              min=0, max=0.2, step=0.005, group="levels"),
        # saved per audio device instead (see main.py), so persist=False here
        Param("latency_ms", "Light delay", "int", cfg.latency_ms,
              min=0, max=500, step=10, unit="ms", group="sync", persist=False),
    ]
