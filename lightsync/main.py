"""Music Light Sync: render loop.

    python -m lightsync.main                 # drive the strip
    python -m lightsync.main --preview       # also draw the strip in the terminal
    python -m lightsync.main --no-wled --preview   # preview only, strip untouched
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import logging.handlers
import sys
import time
from pathlib import Path

import numpy as np

from . import config as config_mod
from .analyzer import BassAnalyzer, DelayLine
from .audio import AudioCapture
from .compositor import Encoder, mix
from .control import Controller
from .layers.color import ColorLayer
from .link import LinkMonitor
from .output import make_sender
from .output_preview import TerminalPreview
from .params import core_params
from .persist import StateStore, load_state
from .web import Telemetry, WebServer

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("lightsync")

N_SAMPLES = 2048
MAX_LATENCY_MS = 500
STATS_EVERY_S = 10.0


def setup_logging(console_level: int) -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(ROOT / "logs" / "lightsync.log",
                                              maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(fmt)
    ch.setLevel(console_level)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(ch)


def main() -> int:
    ap = argparse.ArgumentParser(description="Music Light Sync")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--preview", action="store_true", help="draw the strip in the terminal")
    ap.add_argument("--no-wled", action="store_true", help="don't send anything to WLED")
    ap.add_argument("--duration", type=float, default=0, help="stop after this many seconds (0 = run forever)")
    ap.add_argument("--no-web", action="store_true", help="don't start the web UI")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser on start")
    ap.add_argument("--web-port", type=int, help="override web.port (e.g. for a second test instance)")
    ap.add_argument("--state-file", default=str(ROOT / "state.json"), help="where the live state is saved")
    args = ap.parse_args()

    setup_logging(logging.WARNING if args.preview else logging.INFO)
    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as e:
        log.error("config error: %s", e)
        return 2

    state_path = Path(args.state_file)
    saved = load_state(state_path)
    ctl = Controller(core_params(cfg), cfg.colors, initial=saved.get("params") or {})
    # light delay remembered per audio device: last value used with it wins over config
    learned = {str(k): int(v) for k, v in (saved.get("device_latency_ms") or {}).items()
               if isinstance(v, (int, float)) and 0 <= v <= MAX_LATENCY_MS}
    store = StateStore(state_path, ctl)
    store.device_latency_ms = learned
    store.start()

    telemetry = Telemetry()
    webui = None
    if not args.no_web:
        web_cfg = cfg.web
        if args.no_browser:
            web_cfg = dataclasses.replace(web_cfg, open_browser=False)
        if args.web_port:
            web_cfg = dataclasses.replace(web_cfg, port=args.web_port)
        webui = WebServer(ctl, telemetry, web_cfg, ROOT / "web")
        webui.start()

    audio = AudioCapture()
    analyzer = BassAnalyzer(fps=cfg.fps)
    delay = DelayLine(max_frames=int(MAX_LATENCY_MS * cfg.fps / 1000) + 1)
    color_layer = ColorLayer(cfg.pixels, cfg.fps)
    encoder = Encoder(cfg.pixels)
    tx = None if args.no_wled else make_sender(cfg.protocol, cfg.host, cfg.port, cfg.udp_timeout_s)
    link = None if args.no_wled else LinkMonitor(cfg.host)
    if link is not None:
        link.start()
    preview = TerminalPreview() if args.preview else None
    white = np.zeros(cfg.pixels, np.float32)   # white spot layer arrives in phase 4

    audio.start()
    log.info("running: %d px at %d fps -> %s", cfg.pixels, cfg.fps,
             "preview only" if tx is None else f"{cfg.host}:{cfg.port} ({cfg.protocol})")

    period = 1.0 / cfg.fps
    next_t = time.perf_counter()
    stop_at = next_t + args.duration if args.duration > 0 else float("inf")
    stats_t0 = time.perf_counter()
    frames = late = errors = 0
    work_max = 0.0
    last_err_log = 0.0
    device = ""
    f = None
    frame_no = 0
    fps_ema = float(cfg.fps)
    last_frame_t = time.perf_counter()
    try:
        while time.perf_counter() < stop_at:
            t0 = time.perf_counter()
            fps_ema += (1.0 / max(t0 - last_frame_t, 1e-6) - fps_ema) * 0.05
            last_frame_t = t0
            frame_no += 1
            try:
                # a new audio device brings its own latency (e.g. Bluetooth speaker)
                if audio.device_name and audio.device_name != device:
                    device = audio.device_name
                    ms = learned.get(device, config_mod.latency_for_device(cfg, device))
                    ctl.submit({"op": "set", "key": "latency_ms", "value": ms})
                    log.info("audio device '%s' -> light delay %d ms", device, ms)

                ctl.apply_pending()
                if device and learned.get(device) != ctl["latency_ms"]:
                    learned = {**learned, device: ctl["latency_ms"]}
                    store.device_latency_ms = learned
                f = analyzer.update(audio.latest(N_SAMPLES), audio.rate)
                f = delay.push(f, round(ctl["latency_ms"] * cfg.fps / 1000))

                level = ctl["cozy_color_max"] if ctl["mode"] == "cozy" else ctl["color_max"]
                C = color_layer.render(ctl["color"], level, f, mode=ctl["pulse_mode"],
                                       depth=ctl["pulse_depth"], decay_ms=ctl["pulse_decay_ms"],
                                       sensitivity=ctl["pulse_sensitivity"],
                                       contrast=ctl["pulse_contrast"],
                                       threshold=ctl["pulse_threshold"], hold_ms=ctl["pulse_hold_ms"],
                                       fade_ms=ctl["pulse_fade_ms"])
                pulse = color_layer.pulse.out
                out = mix(C, white, cfg.spot.white_tint, cfg.spot.duck, cfg.spot.overflow)
                out *= 0.0 if ctl["blackout"] else ctl["intensity"]
                rgb = encoder.encode(out)

                if tx is not None:
                    tx.send(rgb)
                if preview is not None:
                    preview.show(rgb, f"pulse {pulse:4.2f}  delay {ctl['latency_ms']} ms  {device}")
                if frame_no % 4 == 0:   # ~15 Hz is plenty for the UI
                    telemetry.publish({"rgb": rgb.tobytes().hex(), "pulse": round(pulse, 3),
                                       "bass": round(f.bass, 3), "silent": f.silent,
                                       "fps": round(fps_ema, 1), "device": device,
                                       "hits": color_layer.pulse.hits,
                                       "link": link.status if link else None})
            except Exception:
                errors += 1
                if time.monotonic() - last_err_log > 5.0:   # isolate the frame, keep running
                    log.exception("frame failed (%d so far)", errors)
                    last_err_log = time.monotonic()

            frames += 1
            work_max = max(work_max, time.perf_counter() - t0)
            next_t += period
            now = time.perf_counter()
            if next_t < now:                 # late: skip ahead, never try to catch up
                late += 1
                next_t = now + period
            time.sleep(max(0.0, next_t - now))

            if now - stats_t0 >= STATS_EVERY_S:
                ls = link.status if link else {}
                log.info("stats: %.1f fps, %d late, work max %.1f ms, frame errors %d, "
                         "send errors %d, link %s %s ms, device '%s', delay %d ms, bass %.2f",
                         frames / (now - stats_t0), late, work_max * 1000, errors,
                         tx.send_errors if tx else 0, ls.get("state", "-"), ls.get("rtt_ms", "-"),
                         device, ctl["latency_ms"], f.bass if f else 0.0)
                stats_t0, frames, late, work_max = now, 0, 0, 0.0
    except KeyboardInterrupt:
        pass
    finally:
        if preview is not None:
            print()
        log.info("stopping")
        if tx is not None:
            tx.black(cfg.pixels)
            tx.close()
        if webui is not None:
            webui.stop()
        if link is not None:
            link.stop()
        store.stop()
        audio.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
