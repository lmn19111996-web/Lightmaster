"""Hardware test for the WLED strip (UDP realtime by default, --protocol ddp for DDP).

    python tools/test_wled.py                 # all steps, asks before each one
    python tools/test_wled.py --step chase    # one step only
    python tools/test_wled.py --no-prompt     # run straight through (burst still needs --yes)

Steps:
    info      WLED version, configured LED count, realtime settings, Wi-Fi signal
    ends      first pixel red, last pixel blue -> confirms count and direction
    chase     one white pixel runs from start to end -> no dead or skipped blocks
    order     whole strip red, green, blue -> confirms color order
    ramp      white fades 0 -> 100 % -> watch for flicker or color shift at the far end
    burst     full white for a few seconds -> PSU / voltage drop check
    fps       60 fps moving gradient -> send timing and the fps WLED reports
    failsafe  stop sending -> WLED should return to its normal look after its timeout
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lightsync.output import PROTOCOLS, make_sender  # noqa: E402

STEPS = ["info", "ends", "chase", "order", "ramp", "burst", "fps", "failsafe"]


def get_json(host: str, path: str, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{host}{path}", timeout=timeout) as r:
            return json.load(r)
    except (OSError, ValueError) as e:
        print(f"  ! could not read http://{host}{path}: {e}")
        return None


def hold(tx, frame: np.ndarray, seconds: float, fps: float = 30) -> None:
    """Keep re-sending a static frame so WLED stays in realtime mode."""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        tx.send(frame)
        time.sleep(1 / fps)


def solid(n: int, rgb) -> np.ndarray:
    return np.tile(np.array(rgb, np.uint8), (n, 1))


# --- steps -------------------------------------------------------------------

def step_info(tx, a):
    info = get_json(a.host, "/json/info")
    cfg = get_json(a.host, "/json/cfg")
    if info:
        leds = info.get("leds", {})
        print(f"  WLED {info.get('ver')} on {info.get('arch')}, uptime {info.get('uptime')} s")
        print(f"  configured LEDs: {leds.get('count')} (script expects {a.pixels})"
              + ("" if leds.get("count") == a.pixels else "  <-- MISMATCH"))
        wifi = info.get("wifi", {})
        print(f"  Wi-Fi RSSI {wifi.get('rssi')} dBm, channel {wifi.get('channel')}")
    if cfg:
        live = cfg.get("if", {}).get("live", {})
        print(f"  realtime enabled:        {live.get('en')}")
        print(f"  realtime timeout:        {live.get('timeout', 0) / 10:.1f} s")
        print(f"  gamma off for realtime:  {live.get('no-gc')}  (want True)")
        print(f"  force max brightness:    {live.get('maxbri')}  (want True, else the WLED slider scales output)")
        abl = cfg.get("hw", {}).get("led", {})
        print(f"  ABL limit:               {abl.get('maxpwr')} mA")


def step_ends(tx, a):
    f = np.zeros((a.pixels, 3), np.uint8)
    f[0] = (255, 0, 0)
    f[-1] = (0, 0, 255)
    print("  First block should be RED, last block BLUE, everything else off.")
    hold(tx, f, a.seconds * 2)


def step_chase(tx, a):
    print("  One white block runs from the red end to the blue end. Watch for gaps.")
    for _ in range(2):
        for i in range(a.pixels):
            f = np.zeros((a.pixels, 3), np.uint8)
            f[i] = (255, 255, 255)
            hold(tx, f, 0.06)


def step_order(tx, a):
    for name, rgb in [("RED", (255, 0, 0)), ("GREEN", (0, 255, 0)), ("BLUE", (0, 0, 255))]:
        print(f"  Strip should be {name}")
        hold(tx, solid(a.pixels, rgb), a.seconds)


def step_ramp(tx, a):
    print("  White fades up from off to full over 6 s.")
    t0 = time.perf_counter()
    while (t := time.perf_counter() - t0) < 6:
        tx.send(solid(a.pixels, (int(255 * t / 6),) * 3))
        time.sleep(1 / 60)
    hold(tx, solid(a.pixels, (255, 255, 255)), 1)


def step_burst(tx, a):
    if not a.yes:
        if a.no_prompt:
            print("  skipped (pass --yes to allow full white)")
            return
        if input(f"  Full white for {a.burst:.0f} s. Type 'yes' to continue: ").strip() != "yes":
            print("  skipped")
            return
    print("  FULL WHITE. Check the far end for a yellow/red tint (voltage drop) and the PSU.")
    hold(tx, solid(a.pixels, (255, 255, 255)), a.burst)
    tx.black(a.pixels)


def step_fps(tx, a):
    duration, fps = 10.0, 60
    print(f"  {fps} fps moving rainbow for {duration:.0f} s, should look smooth.")
    wled_fps = []

    def poll():  # ask WLED mid-stream how fast it is actually updating
        time.sleep(duration / 2)
        info = get_json(a.host, "/json/info", timeout=2)
        if info:
            wled_fps.append(info.get("leds", {}).get("fps"))

    threading.Thread(target=poll, daemon=True).start()

    x = np.arange(a.pixels) / a.pixels
    period = 1 / fps
    sent_at = []
    t0 = next_t = time.perf_counter()
    while (now := time.perf_counter()) - t0 < duration:
        phase = (x + (now - t0) * 0.3) * 2 * np.pi
        rgb = (127.5 * (1 + np.sin(phase[:, None] + np.array([0, 2.094, 4.189])))).astype(np.uint8)
        tx.send(rgb)
        sent_at.append(now)
        next_t += period
        if next_t < time.perf_counter():          # late: skip ahead, don't catch up
            next_t = time.perf_counter() + period
        time.sleep(max(0.0, next_t - time.perf_counter()))

    dt = np.diff(sent_at) * 1000
    print(f"  sent {len(sent_at)} frames: mean {dt.mean():.1f} ms, "
          f"p99 {np.percentile(dt, 99):.1f} ms, max {dt.max():.1f} ms, "
          f"late (>25 ms) {int((dt > 25).sum())}")
    print(f"  WLED-reported fps mid-stream: {wled_fps[0] if wled_fps else 'n/a'}")
    print(f"  send errors so far: {tx.send_errors}")


def step_failsafe(tx, a):
    tx.black(a.pixels)
    print("  Stopped sending. The strip should be black now, then return to its")
    print("  normal WLED look after the realtime timeout (1-3 s).")
    time.sleep(4)


# --- main --------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="192.168.68.50")
    p.add_argument("--protocol", choices=PROTOCOLS, default="udp")
    p.add_argument("--port", type=int, help="default: the protocol's standard port")
    p.add_argument("--pixels", type=int, default=56)
    p.add_argument("--step", choices=STEPS, help="run only this step")
    p.add_argument("--seconds", type=float, default=2.0, help="how long static steps are shown")
    p.add_argument("--burst", type=float, default=3.0, help="full-white burst length in seconds")
    p.add_argument("--yes", action="store_true", help="allow the full-white burst without asking")
    p.add_argument("--no-prompt", action="store_true", help="don't pause between steps")
    a = p.parse_args()

    tx = make_sender(a.protocol, a.host, a.port)
    funcs = {name: globals()[f"step_{name}"] for name in STEPS}
    steps = [a.step] if a.step else STEPS
    try:
        for name in steps:
            if not a.no_prompt and not a.step:
                ans = input(f"\n[{name}] Enter = run, s = skip, q = quit: ").strip().lower()
                if ans == "q":
                    break
                if ans == "s":
                    continue
            else:
                print(f"\n[{name}]")
            funcs[name](tx, a)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        tx.black(a.pixels)
        tx.close()


if __name__ == "__main__":
    main()
