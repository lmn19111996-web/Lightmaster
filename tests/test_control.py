import threading

import pytest

from lightsync.control import CommandError, Controller, Param

PALETTE = {"blue": (0.0, 0.0, 1.0), "red": (1.0, 0.0, 0.0), "green": (0.0, 1.0, 0.0)}


def make():
    return Controller([
        Param("intensity", "Intensity", "float", 1.0, min=0, max=1, step=0.05),
        Param("latency_ms", "Latency", "int", 0, min=0, max=500, step=10),
        Param("blackout", "Blackout", "bool", False),
        Param("mode", "Mode", "choice", "performance", choices=("cozy", "performance")),
        Param("color", "Color", "color", (0.0, 0.0, 1.0)),
    ], PALETTE)


def test_commands_apply_only_on_render_tick():
    c = make()
    c.submit({"op": "set", "key": "intensity", "value": 0.5})
    assert c["intensity"] == 1.0 and c.version == 0
    assert c.apply_pending() is True
    assert c["intensity"] == 0.5 and c.snapshot()["intensity"] == 0.5 and c.version == 1


def test_clamping_and_int_rounding():
    c = make()
    c.submit({"op": "set", "key": "intensity", "value": 7})
    c.submit({"op": "set", "key": "latency_ms", "value": 123.4})
    c.apply_pending()
    assert c["intensity"] == 1.0 and c["latency_ms"] == 123


def test_nudge_toggle_cycle():
    c = make()
    for _ in range(3):
        c.submit({"op": "nudge", "key": "latency_ms", "delta": 200})
    c.submit({"op": "toggle", "key": "blackout"})
    c.submit({"op": "cycle", "key": "mode", "dir": 1})
    c.submit({"op": "cycle", "key": "color", "dir": -1})
    c.apply_pending()
    assert c["latency_ms"] == 500
    assert c["blackout"] is True
    assert c["mode"] == "cozy"
    assert c["color"] == (0.0, 1.0, 0.0)   # blue -> wraps back to green


def test_no_change_no_version_bump():
    c = make()
    c.submit({"op": "set", "key": "intensity", "value": 1.0})
    assert c.apply_pending() is False and c.version == 0


@pytest.mark.parametrize("cmd", [
    {"op": "set", "key": "nope", "value": 1},
    {"op": "set", "key": "intensity", "value": "loud"},
    {"op": "set", "key": "intensity", "value": True},
    {"op": "set", "key": "intensity", "value": float("nan")},
    {"op": "set", "key": "mode", "value": "disco"},
    {"op": "set", "key": "color", "value": [1, 0]},
    {"op": "toggle", "key": "intensity"},
    {"op": "nudge", "key": "blackout", "delta": 1},
    {"op": "cycle", "key": "intensity"},
    {"op": "explode", "key": "intensity"},
    "not a dict",
])
def test_invalid_commands_rejected_immediately(cmd):
    c = make()
    with pytest.raises(CommandError):
        c.submit(cmd)
    assert c.apply_pending() is False


def test_snapshot_is_read_only_and_stable():
    c = make()
    snap = c.snapshot()
    with pytest.raises(TypeError):
        snap["intensity"] = 0  # type: ignore[index]
    c.submit({"op": "set", "key": "intensity", "value": 0.2})
    c.apply_pending()
    assert snap["intensity"] == 1.0          # old snapshot unchanged
    assert c.snapshot()["intensity"] == 0.2


def test_initial_values_restored_and_bad_ones_ignored():
    c = Controller(make().params.values(), PALETTE,
                   initial={"intensity": 0.3, "mode": "disco", "gone": 1})
    assert c["intensity"] == 0.3 and c["mode"] == "performance"


def test_schema_is_json_friendly():
    import json
    s = make().schema()
    json.dumps(s)
    assert {p["key"] for p in s["params"]} >= {"intensity", "color"}


def test_submit_from_many_threads():
    c = make()
    threads = [threading.Thread(target=lambda: [c.submit({"op": "nudge", "key": "latency_ms", "delta": 1})
                                                for _ in range(50)]) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    c.apply_pending()
    assert c["latency_ms"] == 200
