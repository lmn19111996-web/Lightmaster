from pathlib import Path

import numpy as np
import pytest

from lightsync import config as config_mod
from lightsync.analyzer import BassAnalyzer, DelayLine

ROOT = Path(__file__).resolve().parent.parent
RATE = 48000


def tone(freq, amp, n=2048):
    t = np.arange(n) / RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_bass_reacts_to_bass_not_treble():
    a = BassAnalyzer(fps=60)
    for _ in range(30):
        f = a.update(tone(80, 0.5), RATE)
    assert f.bass > 0.9 and not f.silent
    b = BassAnalyzer(fps=60)
    for _ in range(30):
        g = b.update(tone(3000, 0.5), RATE)
    assert g.bass < 0.05


def test_silence_decays_to_zero():
    a = BassAnalyzer(fps=60)
    for _ in range(30):
        a.update(tone(80, 0.5), RATE)
    for _ in range(120):
        f = a.update(np.zeros(2048, np.float32), RATE)
    assert f.silent and f.bass == 0.0


def test_auto_gain_quiet_and_loud_look_alike():
    loud, quiet = BassAnalyzer(fps=60), BassAnalyzer(fps=60)
    for _ in range(60):
        fl = loud.update(tone(80, 0.8), RATE)
        fq = quiet.update(tone(80, 0.05), RATE)
    assert abs(fl.bass - fq.bass) < 0.05


def test_delay_line():
    d = DelayLine(max_frames=3)
    out = [d.push(i, 2) for i in range(6)]
    assert out == [0, 0, 0, 1, 2, 3]
    assert d.push(99, 50) == 3          # clamped to what's buffered
    assert d.push(100, 0) == 100


def test_shipped_config_loads():
    cfg = config_mod.load(ROOT / "config.yaml")
    assert cfg.pixels == 56 and cfg.host == "192.168.68.50"
    assert config_mod.latency_for_device(cfg, "Lautsprecher (ULT FIELD 1)") == 200
    assert config_mod.latency_for_device(cfg, "Surface speakers") == cfg.latency_ms


def test_bad_config_gives_clear_error(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("wled: {host: x}\nstrip: {pixels: 56}\ncolor: nope\ncolors: {red: [1, 0, 0]}\n")
    with pytest.raises(config_mod.ConfigError, match="unknown name 'nope'"):
        config_mod.load(p)
    p.write_text("wled: {host: x}\nstrip: {pixels: 56}\ncolor: red\ncolors: {red: [1, 0, 0]}\n"
                 "web: {lan: true}\n")
    with pytest.raises(config_mod.ConfigError, match="pin"):
        config_mod.load(p)


# --- pulse (color layer) ---------------------------------------------------

from lightsync.layers.color import ColorLayer, Pulse  # noqa: E402
from lightsync.analyzer import Features  # noqa: E402


def run_pulse(levels, **kw):
    p = Pulse(fps=60)
    args = dict(sensitivity=1.0, decay_ms=150, contrast=1.0) | kw
    return [p.smooth(x, **args) for x in levels]


def test_pulse_attack_fast_decay_slow():
    out = run_pulse([1.0] * 3 + [0.0] * 60)
    assert out[2] > 0.9                         # 10 ms attack: full within a few frames
    assert 0.2 < out[3 + 9] < 0.5               # ~150 ms decay: about 1/e after 150 ms
    assert out[-1] < 0.01


def test_sensitivity_and_contrast():
    soft = [0.5] * 30
    assert abs(run_pulse(soft)[-1] - 0.5) < 0.01
    assert run_pulse(soft, sensitivity=2.0)[-1] > 0.99          # soft hits now reach full
    assert abs(run_pulse(soft, contrast=2.0)[-1] - 0.25) < 0.01  # quiet parts pushed down


def test_depth_sets_the_dip():
    f_off, f_on = Features(bass=0.0, silent=False), Features(bass=1.0, silent=False)
    kw = dict(sensitivity=1.0, contrast=1.0, decay_ms=150)
    for depth, floor in [(0.0, 0.2), (0.9, 0.02), (1.0, 0.0)]:
        layer = ColorLayer(4, fps=60)
        for _ in range(120):
            C = layer.render((1.0, 1.0, 1.0), 0.2, f_off, depth=depth, **kw)
        assert np.allclose(C, floor, atol=1e-3)
        for _ in range(10):
            C = layer.render((1.0, 1.0, 1.0), 0.2, f_on, depth=depth, **kw)
        assert np.allclose(C, 0.2, atol=2e-3)   # full hit always reaches the color level


# --- link monitor ------------------------------------------------------------

from lightsync.link import classify, parse_ping  # noqa: E402


def test_parse_ping_locales():
    assert parse_ping("Antwort von 192.168.68.50: Bytes=32 Zeit=8ms TTL=255") == 8
    assert parse_ping("Antwort von 192.168.68.50: Bytes=32 Zeit<1ms TTL=255") == 1
    assert parse_ping("Reply from 192.168.68.50: bytes=32 time=123ms TTL=255") == 123
    assert parse_ping("64 bytes from 192.168.68.50: icmp_seq=1 ttl=255 time=7.4 ms") == 7
    assert parse_ping("Zeitüberschreitung der Anforderung.") is None


def test_classify():
    assert classify([]) == ("unknown", None, 0.0)
    assert classify([8, 9, 7, 10, 8])[0] == "good"
    assert classify([8, None, 7, 10, 8])[0] == "slow"          # any loss -> not good
    assert classify([80, 90, 70, 60, 85])[0] == "slow"
    assert classify([300, 280, 250, 310, 290])[0] == "bad"
    assert classify([8, None, None, None, 9])[0] == "bad"      # heavy loss
    assert classify([None] * 5)[0] == "down"


# --- beat style ----------------------------------------------------------------

def kicks(n_kicks, base=0.1, peak=0.9, every=30, width=3, tail=30):
    seq = []
    for _ in range(n_kicks):
        seq += [peak] * width + [base] * (every - width)
    return seq + [base] * tail


def run_beat(levels, threshold=0.5, hold_ms=80, decay_ms=150):
    p = Pulse(fps=60)
    out = [p.beat(x, threshold, hold_ms, decay_ms) for x in levels]
    return p, out


def test_beat_fires_once_per_kick():
    p, _ = run_beat([0.1] * 60 + kicks(8))
    assert p.hits == 8


def test_sustained_bass_fires_once_not_continuously():
    p, out = run_beat([0.1] * 60 + [0.9] * 120)
    assert p.hits == 1
    assert out[-1] < 0.01                       # light goes back down while the bass just sits there


def test_kicks_on_top_of_a_bassline():
    p, _ = run_beat([0.6] * 120)
    assert p.hits == 1                          # the bassline starting is one hit
    for x in kicks(6, base=0.6, peak=0.95):
        p.beat(x, 0.5, 80, 150)
    assert p.hits == 1 + 6                      # then every kick on top of it


def test_below_threshold_never_fires():
    p, out = run_beat([0.1] * 60 + kicks(6, peak=0.4))
    assert p.hits == 0 and max(out) == 0.0


def test_rate_limited_to_10_per_second():
    p, _ = run_beat([0.1] * 60 + [0.9, 0.1] * 60)   # alternates every frame for 2 s
    assert p.hits <= 21


def test_hold_then_decay_two_states():
    _, out = run_beat([0.1] * 60 + [0.9] * 3 + [0.1] * 60, hold_ms=100, decay_ms=20)
    hit = 60
    assert all(v == 1.0 for v in out[hit:hit + 6])        # held fully on for ~100 ms
    assert out[hit + 12] < 0.05                            # then quickly back off: two clear states
    assert all(v == 0.0 for v in out[:hit])
