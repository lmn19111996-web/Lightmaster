import asyncio
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp
import pytest

from lightsync.config import WebConfig
from lightsync.control import Controller, Param
from lightsync.persist import StateStore, load_state
from lightsync.web import Telemetry, WebServer

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_ctl():
    return Controller([
        Param("intensity", "Intensity", "float", 1.0, min=0, max=1, step=0.01),
        Param("blackout", "Blackout", "bool", False, persist=False),
        Param("color", "Color", "color", (0.0, 0.0, 1.0)),
    ], {"blue": (0, 0, 1), "red": (1, 0, 0)})


@pytest.fixture
def server(request):
    pin = getattr(request, "param", "")
    ctl = make_ctl()
    tel = Telemetry()
    tel.publish({"bass": 0.5})
    srv = WebServer(ctl, tel, WebConfig(port=free_port(), lan=bool(pin), pin=pin, open_browser=False),
                    ROOT / "web")
    assert srv.start()
    srv.url = f"http://127.0.0.1:{srv.port}/"   # lan mode binds 0.0.0.0, still reachable locally
    yield srv, ctl
    srv.stop()


def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_index_schema_state(server):
    srv, ctl = server
    status, body = http(srv.url)
    assert status == 200 and b"Lightmaster" in body
    status, body = http(srv.url + "api/schema")
    assert status == 200 and {p["key"] for p in json.loads(body)["params"]} == {"intensity", "blackout", "color"}
    status, body = http(srv.url + "api/state")
    assert json.loads(body)["state"]["intensity"] == 1.0


def test_post_command_reaches_controller(server):
    srv, ctl = server
    assert http(srv.url + "api/command", {"op": "set", "key": "intensity", "value": 0.3})[0] == 200
    assert http(srv.url + "api/command", {"op": "set", "key": "intensity", "value": "x"})[0] == 400
    ctl.apply_pending()
    assert ctl["intensity"] == 0.3


def test_foreign_origin_refused(server):
    srv, _ = server
    status, _ = http(srv.url + "api/command", {"op": "toggle", "key": "blackout"},
                     headers={"Origin": "http://evil.example"})
    assert status == 403


@pytest.mark.parametrize("server", ["4321"], indirect=True)
def test_pin_required_in_lan_mode(server):
    srv, _ = server
    assert http(srv.url + "api/state")[0] == 401
    assert http(srv.url + "api/state?pin=0000")[0] == 401
    assert http(srv.url + "api/state?pin=4321")[0] == 200
    assert http(srv.url, )[0] == 200   # the page itself loads, then asks for the PIN


def test_websocket_hello_cmd_ack_and_push(server):
    srv, ctl = server

    async def run():
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(srv.url + "ws") as ws:
                hello = await ws.receive_json(timeout=3)
                assert hello["type"] == "hello" and hello["telemetry"]["bass"] == 0.5
                await ws.send_json({"type": "cmd", "id": 1, "cmd": {"op": "toggle", "key": "blackout"}})
                await ws.send_json({"type": "cmd", "id": 2, "cmd": {"op": "toggle", "key": "intensity"}})
                acks, state = {}, None
                for _ in range(40):
                    m = await ws.receive_json(timeout=3)
                    if m["type"] == "ack":
                        acks[m["id"]] = m
                        if m["id"] == 1:
                            ctl.apply_pending()   # stands in for the render loop
                    if m["type"] == "state" and m["state"]["blackout"]:
                        state = m
                    if len(acks) == 2 and state:
                        break
                assert acks[1]["ok"] and not acks[2]["ok"]
                assert state is not None

    asyncio.run(run())


def test_port_in_use_does_not_crash():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        srv = WebServer(make_ctl(), Telemetry(), WebConfig(port=port, open_browser=False), ROOT / "web")
        assert srv.start() is False
        srv.stop()


def test_state_store_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    ctl = make_ctl()
    store = StateStore(path, ctl)
    store.start()
    ctl.submit({"op": "set", "key": "intensity", "value": 0.4})
    ctl.submit({"op": "toggle", "key": "blackout"})
    ctl.apply_pending()
    store.device_latency_ms = {"ULT FIELD": 180}
    store.stop()
    data = load_state(path)
    assert data["params"] == {"intensity": 0.4, "color": [0.0, 0.0, 1.0]}   # blackout not saved
    assert data["device_latency_ms"] == {"ULT FIELD": 180}
    restored = Controller(make_ctl().params.values(), initial=data["params"])
    assert restored["intensity"] == 0.4


def test_corrupt_state_file_is_ignored(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    assert load_state(p) == {}
