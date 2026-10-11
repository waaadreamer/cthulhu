"""Websocket framing shared by the temple, galaxies and agents.

Frames are JSON objects with a "type":
  hello    client -> temple   {kind: agent|galaxy, id, token}
  welcome  temple -> client   identity and initial state
  call     client -> temple   {id, method, params}  answered by "result"
  rpc      temple -> client   {id, method, params}  answered by "rpc_result"
  anything else is a one-way event.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
from typing import Any, Awaitable, Callable

import websockets

log = logging.getLogger(__name__)


def agent_token(secret: str, agent_id: str, epoch: int) -> str:
    return hmac.new(secret.encode(), f"agent:{agent_id}:{epoch}".encode(), hashlib.sha256).hexdigest()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def dumps(frame: dict) -> str:
    return json.dumps(frame, ensure_ascii=False, default=str)


class CallFailed(Exception):
    pass


class Link:
    """A persistent, self-reconnecting client connection to the temple."""

    def __init__(self, url: str, hello: dict,
                 on_welcome: Callable[[dict], Awaitable[None]],
                 on_frame: Callable[[dict], Awaitable[Any]]):
        self.url, self.hello = url, hello
        self.on_welcome, self.on_frame = on_welcome, on_frame
        self.ws = None
        self.connected = asyncio.Event()
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 0
        self.fatal: str | None = None

    async def run(self) -> None:
        delay = 0.5
        while self.fatal is None:
            try:
                async with websockets.connect(self.url, max_size=64 * 1024 * 1024, ping_interval=20,
                                              ping_timeout=60, proxy=None) as ws:
                    await ws.send(dumps({"type": "hello", **self.hello}))
                    welcome = json.loads(await ws.recv())
                    if welcome.get("type") == "rejected":
                        self.fatal = welcome.get("reason", "rejected")
                        log.error("temple rejected connection: %s", self.fatal)
                        return
                    self.ws = ws
                    self.connected.set()
                    delay = 0.5
                    await self.on_welcome(welcome)
                    async for raw in ws:
                        frame = json.loads(raw)
                        if frame.get("type") == "result":
                            fut = self.pending.pop(frame["id"], None)
                            if fut and not fut.done():
                                fut.set_result(frame)
                        else:
                            asyncio.create_task(self._dispatch(frame))
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
                log.warning("temple link down: %s", e)
            finally:
                self.ws = None
                self.connected.clear()
                for fut in self.pending.values():
                    if not fut.done():
                        fut.set_exception(CallFailed("temple connection lost"))
                self.pending.clear()
            await asyncio.sleep(delay * (1 + random.random()))
            delay = min(delay * 2, 30)

    async def _dispatch(self, frame: dict) -> None:
        try:
            value = await self.on_frame(frame)
            if frame.get("type") == "rpc":
                await self.emit({"type": "rpc_result", "id": frame["id"], "ok": True, "result": value})
        except Exception as e:
            log.exception("frame handler failed")
            if frame.get("type") == "rpc":
                await self.emit({"type": "rpc_result", "id": frame["id"], "ok": False, "error": str(e)})

    async def emit(self, frame: dict) -> bool:
        ws = self.ws
        if ws is None:
            return False
        try:
            await ws.send(dumps(frame))
            return True
        except websockets.WebSocketException:
            return False

    async def call(self, method: str, timeout: float = 300, **params) -> Any:
        await asyncio.wait_for(self.connected.wait(), timeout)
        self.next_id += 1
        cid = self.next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        if not await self.emit({"type": "call", "id": cid, "method": method, "params": params}):
            self.pending.pop(cid, None)
            raise CallFailed("temple connection lost")
        frame = await asyncio.wait_for(fut, timeout)
        if not frame.get("ok"):
            raise CallFailed(frame.get("error", "call failed"))
        return frame.get("result")
