"""Web UI server: aiohttp in its own thread with its own event loop.

It only talks to the render loop through the Controller (submit commands, read
snapshots) and the Telemetry object (read the latest dict). If anything in here
fails, the lights keep running.

    GET  /               the UI (web/index.html)
    GET  /api/schema     parameters + palette
    GET  /api/state      {"version", "state"}
    POST /api/command    one command or a list of commands
    GET  /ws             WebSocket: hello, state pushes on change, telemetry at PUSH_HZ,
                         client sends {"type": "cmd", "id": n, "cmd": {...}} -> ack

Security: binds to 127.0.0.1 unless web.lan is true, which requires a PIN
(?pin=... or X-Pin header). Requests from other websites (foreign Origin) are refused.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import threading
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

from .config import WebConfig
from .control import CommandError, Controller

log = logging.getLogger(__name__)

PUSH_HZ = 15
SEND_TIMEOUT_S = 0.5


class Telemetry:
    """Latest live values from the render loop. The dict is replaced, never mutated."""

    def __init__(self):
        self.data: dict = {}

    def publish(self, data: dict) -> None:
        self.data = data


class WebServer:
    def __init__(self, ctl: Controller, telemetry: Telemetry, cfg: WebConfig, static_dir: Path):
        self.ctl = ctl
        self.telemetry = telemetry
        self.host = "0.0.0.0" if cfg.lan else "127.0.0.1"
        self.port = cfg.port
        self.pin = cfg.pin.encode() if cfg.lan else b""
        self.open_browser = cfg.open_browser
        self.static_dir = static_dir
        self.url = f"http://127.0.0.1:{cfg.port}/"
        self.running = False
        self._clients: set[web.WebSocketResponse] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_ev: asyncio.Event | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="web", daemon=True)

    # --- control from the main thread ---------------------------------------

    def start(self) -> bool:
        self._thread.start()
        self._ready.wait(timeout=5)
        if self.running and self.open_browser:
            try:
                webbrowser.open(self.url)
            except Exception:
                pass
        return self.running

    def stop(self) -> None:
        if self._loop is not None and self._stop_ev is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_ev.set)
            except RuntimeError:   # loop already closed (e.g. the server never started)
                pass
        self._thread.join(timeout=3)

    # --- server thread ------------------------------------------------------

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:
            log.exception("web UI crashed (the lights keep running)")
        finally:
            self.running = False
            self._ready.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_ev = asyncio.Event()
        app = web.Application(middlewares=[self._guard])
        app.add_routes([
            web.get("/", self._index),
            web.get("/api/schema", self._schema),
            web.get("/api/state", self._state),
            web.post("/api/command", self._command),
            web.get("/ws", self._ws),
        ])
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.host, self.port).start()
        except OSError as e:
            log.error("web UI: cannot listen on %s:%d (%s); running without UI", self.host, self.port, e)
            await runner.cleanup()
            return
        self.running = True
        self._ready.set()
        log.info("web UI at %s%s", self.url, f" (LAN, port {self.port})" if self.pin else "")

        pusher = asyncio.create_task(self._push_loop())
        await self._stop_ev.wait()
        pusher.cancel()
        for ws in list(self._clients):
            await ws.close()
        await runner.cleanup()

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        if request.path.startswith("/api/") or request.path == "/ws":
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.host:
                return web.json_response({"error": "foreign origin"}, status=403)
            if self.pin:
                given = (request.query.get("pin") or request.headers.get("X-Pin") or "").encode()
                if not hmac.compare_digest(given, self.pin):
                    return web.json_response({"error": "pin required"}, status=401)
        return await handler(request)

    # --- handlers -----------------------------------------------------------

    async def _index(self, request):
        return web.FileResponse(self.static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    async def _schema(self, request):
        return web.json_response({**self.ctl.schema(), "pin_required": bool(self.pin)})

    async def _state(self, request):
        return web.json_response(self._state_msg())

    async def _command(self, request):
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        cmds = body if isinstance(body, list) else [body]
        try:
            for cmd in cmds:
                self.ctl.submit(cmd)
        except CommandError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True})

    async def _ws(self, request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        await ws.send_str(json.dumps({**self._state_msg(), "type": "hello", "schema": self.ctl.schema(),
                                      "telemetry": self.telemetry.data}))
        self._clients.add(ws)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    m = json.loads(msg.data)
                    if not isinstance(m, dict) or m.get("type") != "cmd":
                        continue
                    self.ctl.submit(m.get("cmd"))
                    reply = {"type": "ack", "id": m.get("id"), "ok": True}
                except (ValueError, CommandError) as e:
                    reply = {"type": "ack", "id": m.get("id") if isinstance(m, dict) else None,
                             "ok": False, "error": str(e)}
                await ws.send_str(json.dumps(reply))
        finally:
            self._clients.discard(ws)
        return ws

    # --- pushes -------------------------------------------------------------

    def _state_msg(self) -> dict:
        version = self.ctl.version
        return {"type": "state", "version": version, "state": dict(self.ctl.snapshot())}

    async def _push_loop(self) -> None:
        last_version = -1
        while True:
            await asyncio.sleep(1 / PUSH_HZ)
            if not self._clients:
                continue
            msgs = []
            if self.ctl.version != last_version:
                st = self._state_msg()
                last_version = st["version"]
                msgs.append(json.dumps(st))
            msgs.append(json.dumps({"type": "telemetry", **self.telemetry.data}))
            for ws in list(self._clients):
                try:
                    for m in msgs:
                        await asyncio.wait_for(ws.send_str(m), SEND_TIMEOUT_S)
                except Exception:   # slow or dead client: drop it, never stall the others
                    self._clients.discard(ws)
                    asyncio.create_task(ws.close())
