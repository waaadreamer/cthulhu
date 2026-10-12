"""Native runtimes' loopback-only, scoped bridge to temple's Galaxy API.

The local token authenticates only this process' loopback bridge. It is not a
company credential; upstream configuration and authentication exist on Ali only.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import socket
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .api_provider import MAX_REQUEST, IDLE_TIMEOUT, request_body
from .runtime import CodexRuntime, RuntimeError_


class GalaxyAPITransport:
    """Shared loopback transport; the native runtime retains execution and tools."""
    def __init__(self, *, link, provider, state_dir, model, protocol="responses"):
        self.model, self.protocol = model, protocol
        self.link = link
        self.provider = provider
        scope = hashlib.sha256(str(provider).encode()).hexdigest()[:16]
        self.api_home = Path(state_dir) / "galaxy-api" / scope
        self.local_token = secrets.token_hex(32)
        self.server = None
        self.server_task = None
        self.stream_ids: set[str] = set()
        self.api_result = {}

    async def start(self):
        if self.server_task is not None:
            return
        if self.link is None or not self.provider:
            raise RuntimeError_("galaxy_api requires an authenticated link and provider")
        catalog = await self.link.call("api_catalog", timeout=15)
        backend = "claude" if self.protocol == "messages" else "codex"
        if not any(p["id"] == self.provider and self.model in p["models"] and
                   p.get("model_backends", {}).get(self.model, "codex") == backend for p in catalog):
            raise RuntimeError_("eligible Galaxy API provider is offline or does not offer this model")
        self.api_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.server_task is None:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(16)
            app = Starlette(routes=[Route("/v1/" + self.protocol, self.responses, methods=["POST"])])
            self.server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off"))
            # This server lives inside an agent; it must not install process signal handlers.
            self.server.capture_signals = _no_signals
            self.server_task = asyncio.create_task(self.server.serve(sockets=[sock]))
            self.api_transport = {"home": str(self.api_home), "token": self.local_token,
                                  "base_url": f"http://127.0.0.1:{sock.getsockname()[1]}/v1"}
            for _ in range(100):
                if self.server.started:
                    break
                if self.server_task.done():
                    await self.server_task
                    raise RuntimeError_("local API bridge failed")
                await asyncio.sleep(0.01)

    async def responses(self, request: Request):
        auth = request.headers.get("x-api-key", "") if self.protocol == "messages" else ""
        if not (secrets.compare_digest(auth, self.local_token) or
                secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + self.local_token)):
            return JSONResponse({"error": {"message": "unauthorized local API caller"}}, status_code=401)
        data = b""
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_REQUEST:
                return JSONResponse({"error": {"message": "API request too large"}}, status_code=413)
        try:
            body = request_body(json.loads(data), [self.model], self.protocol)
            opened = await self.link.call("api_open", provider=self.provider, body=body, protocol=self.protocol,
                                          beta=request.headers.get("anthropic-beta", "") if self.protocol == "messages" else "",
                                          timeout=20)
        except Exception:
            return JSONResponse({"error": {"message": "Galaxy API route unavailable"}}, status_code=503)
        sid = opened["id"]
        self.stream_ids.add(sid)
        self.api_result = {k: v for k, v in opened.items() if k != "id"} | {"request_id": sid, "request_dispatched": True}

        async def events():
            try:
                while True:
                    part = await self.link.call("api_next", id=sid, timeout=IDLE_TIMEOUT + 10)
                    if part.get("meta"):
                        self.api_result.update(part["meta"])
                    if part.get("error"):
                        yield failure_event(part["error"], self.protocol)
                        break
                    if part.get("data"):
                        yield base64.b64decode(part["data"])
                    if part.get("done"):
                        break
            except asyncio.CancelledError:
                raise
            except Exception:
                yield failure_event("transport_disconnected", self.protocol)
            finally:
                self.stream_ids.discard(sid)
                if self.link.connected.is_set():
                    try:
                        await self.link.call("api_cancel", id=sid, timeout=5)
                    except Exception:
                        pass
        return StreamingResponse(events(), media_type="text/event-stream")

    async def close(self):
        for sid in list(self.stream_ids):
            if self.link.connected.is_set():
                try:
                    await self.link.call("api_cancel", id=sid, timeout=5)
                except Exception:
                    pass
        if self.server:
            self.server.should_exit = True
        if self.server_task:
            try:
                await asyncio.wait_for(self.server_task, 10)
            except asyncio.TimeoutError:
                self.server_task.cancel()
            self.server_task = None


class GalaxyAPIRuntime(CodexRuntime):
    def __init__(self, codex, cwd, *, link, provider, state_dir, **kwargs):
        super().__init__(codex, cwd, log_path=str(Path(state_dir) / "galaxy-api.log"), **kwargs)
        self.transport = GalaxyAPITransport(link=link, provider=provider, state_dir=state_dir, model=self.model)
        self.turn_lock = asyncio.Lock()

    @property
    def api_result(self):
        return self.transport.api_result

    async def responses(self, request):
        return await self.transport.responses(request)

    async def start(self):
        if self.alive:
            return
        await self.transport.start()
        self.api_transport = self.transport.api_transport
        await super().start()

    async def run_turn(self, *args, **kwargs):
        async with self.turn_lock:
            return await super().run_turn(*args, **kwargs)

    async def close(self):
        try:
            await super().close()
        finally:
            await self.transport.close()


def failure_event(code, protocol="responses"):
    if protocol == "messages":
        return ("event: error\ndata: " + json.dumps({"type": "error", "error": {
            "type": "api_error", "message": "Galaxy API: " + code}}) + "\n\n").encode()
    return ("data: " + json.dumps({"type": "response.failed", "response": {
        "id": "transport_failure", "status": "failed",
        "error": {"code": "server_error", "message": "Galaxy API: " + code}}}) + "\n\n").encode()


from contextlib import contextmanager

@contextmanager
def _no_signals():
    yield
