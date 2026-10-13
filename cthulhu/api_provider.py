"""Versioned native model transport over existing authenticated galaxy links.

Company transport configuration and credentials are consumed only by ProviderWorker.
Broker holds bounded, transient streams and a public, allowlisted capability catalog.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import ssl
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .roles import Denied, Role

API_VERSION = 1
MAX_REQUEST = 4 * 1024 * 1024
MAX_CHUNK = 64 * 1024
MAX_EVENT = 4 * 1024 * 1024
MAX_STREAM = 32 * 1024 * 1024
MAX_STREAMS = 256
MAX_PROVIDER = 64
MAX_CALLER = 8
SLOT_WAIT = 15
STREAM_TTL = 600
IDLE_TIMEOUT = 90
QUEUE_SIZE = 4
CANCEL_TIMEOUT = 5
ERRORS = {"provider_disconnected", "caller_disconnected", "timeout", "cancelled",
          "upstream_auth", "upstream_rate_limit", "upstream_rejected", "upstream_unavailable",
          "invalid_stream", "too_large", "backpressure", "provider_error"}
NAME = re.compile(r"^[a-zA-Z0-9_./:-]{1,128}$")
BETA = re.compile(r"^[a-zA-Z0-9,._-]{0,2048}$")
REQUEST_FIELDS = {"model", "input", "instructions", "tools", "tool_choice", "parallel_tool_calls",
                  "reasoning", "text", "service_tier", "max_output_tokens", "include", "stream",
                  "store", "truncation", "prompt_cache_key", "prompt_cache_retention",
                  "temperature", "top_p", "client_metadata"}
MESSAGE_FIELDS = {"model", "messages", "system", "tools", "tool_choice", "max_tokens", "stream",
                  "thinking", "output_config", "temperature", "top_p", "top_k", "stop_sequences",
                  "metadata", "service_tier", "context_management"}


def json_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


def request_body(body: dict, models: list[str], protocol="responses") -> dict:
    if not isinstance(body, dict) or json_size(body) > MAX_REQUEST:
        raise ValueError("invalid API request size")
    if body.get("model") not in models:
        raise Denied("model not offered")
    if protocol == "messages":
        if set(body) - MESSAGE_FIELDS or not isinstance(body.get("messages"), list):
            raise ValueError("unsupported Messages request fields")
        return body | {"stream": True}
    if protocol != "responses":
        raise ValueError("unsupported provider protocol")
    if body.get("service_tier", "default") not in ("default", "auto", "fast", "priority"):
        raise ValueError("unsupported requested service tier")
    unknown = set(body) - REQUEST_FIELDS
    if unknown:
        # No URLs, headers, provider-local credentials, server-side history handles.
        raise ValueError("unsupported Responses request fields")
    if not isinstance(body.get("input"), (str, list)):
        raise ValueError("input must contain complete conversation history")
    return {k: v for k, v in body.items() if k != "client_metadata"} | {"stream": True, "store": False}


@dataclass
class Stream:
    id: str
    caller: object
    provider: object
    model: str
    tier: str
    protocol: str = "responses"
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))
    total: int = 0
    ended: bool = False
    reading: bool = False
    pushing: bool = False
    seq: int = 0
    timer: asyncio.Task | None = None
    cancel_ack: asyncio.Event = field(default_factory=asyncio.Event)


class ProviderBroker:
    def __init__(self, temple):
        self.temple = temple
        self.providers: dict[str, dict] = {}
        self.streams: dict[str, Stream] = {}

    def policy(self, galaxy: str) -> dict:
        return self.temple.config.get("api_providers", {}).get(galaxy, {})

    def register(self, conn, p: dict) -> dict:
        policy = self.policy(conn.id)
        if conn.kind != "galaxy" or self.temple.galaxies.get(conn.id) is not conn or not policy:
            raise Denied("galaxy is not authorized as an API provider")
        if p.get("version") != API_VERSION or p.get("protocol") != "responses":
            raise ValueError("unsupported provider protocol")
        offered = p.get("models")
        if not isinstance(offered, list) or len(offered) > 128 or not all(isinstance(m, str) and NAME.fullmatch(m) for m in offered):
            raise ValueError("invalid model catalog")
        models = sorted(set(offered) & set(policy.get("models", [])))
        if not models:
            raise Denied("no permitted models")
        backends = p.get("model_backends") or {}
        if not isinstance(backends, dict) or any(k not in offered or v not in ("codex", "claude")
                                                for k, v in backends.items()):
            raise ValueError("invalid model backend catalog")
        if "claude" in backends.values() and "galaxy_api_claude_v1" not in conn.capabilities:
            raise Denied("provider lacks native Claude capability")
        self.providers[conn.id] = {"conn": conn, "models": models, "version": API_VERSION,
                                   "model_backends": {m: backends.get(m, "codex") for m in models},
                                   "revision": str(p.get("revision", ""))[:64]}
        return {"version": API_VERSION, "models": models}

    def catalog(self, consumer: str | None = None) -> list[dict]:
        return [{"id": gid, "models": item["models"], "protocol": "responses", "version": API_VERSION,
                 "model_backends": item["model_backends"],
                 "revision": item["revision"], "requested_tiers": ["default", "priority", "fast"],
                 "fast_verified": False}
                for gid, item in sorted(self.providers.items())
                if self.temple.galaxies.get(gid) is item["conn"] and
                (consumer is None or consumer in self.policy(gid).get("consumers", []))]

    def policy_ids(self, consumer: str) -> list[str]:
        return [g for g, v in self.temple.config.get("api_providers", {}).items() if consumer in v.get("consumers", [])]

    def consumer(self, conn) -> str:
        if conn.kind == "maintenance" and conn in self.temple.maintainers:
            maintainer = self.temple.god(Role.GHROTH)
            if maintainer:
                return maintainer["galaxy_id"]
            raise Denied("maintenance consumer is not configured")
        if conn.kind == "agent" and self.temple.agents.get(conn.id) is conn:
            agent = self.temple.store.agent(conn.id)
            if agent and agent["desired"] == "running":
                return agent["galaxy_id"]
        # A galaxy may perform operator-controlled transport canaries as itself.
        if conn.kind == "galaxy" and self.temple.galaxies.get(conn.id) is conn:
            return conn.id
        raise Denied("stale or unauthorized API caller")

    async def open(self, conn, p: dict) -> dict:
        consumer = self.consumer(conn)
        if "galaxy_api_v1" not in getattr(conn, "capabilities", []):
            raise Denied("caller lacks Galaxy API capability")
        gid = p.get("provider")
        # A restarting provider re-registers within seconds; wait instead of failing the turn.
        for _ in range(SLOT_WAIT * 10 + 1):
            offered = next((x for x in self.catalog(consumer) if x["id"] == gid), None)
            if offered or gid not in self.policy_ids(consumer):
                break
            await asyncio.sleep(.1)
        if not offered:
            raise Denied("eligible API provider is not online")
        protocol = p.get("protocol", "responses")
        if protocol == "messages" and "galaxy_api_claude_v1" not in conn.capabilities:
            raise Denied("caller lacks native Claude capability")
        body = request_body(p.get("body"), offered["models"], protocol)
        backend = offered["model_backends"][body["model"]]
        if protocol != ("messages" if backend == "claude" else "responses"):
            raise Denied("model requires its native backend protocol")
        beta = p.get("beta", "")
        if not isinstance(beta, str) or not BETA.fullmatch(beta):
            raise ValueError("invalid Anthropic beta header")
        policy, limits = self.policy(gid), self.temple.config.get("api_limits", {})
        limit = policy.get("max_streams", limits.get("per_provider", MAX_PROVIDER))
        per_caller = policy.get("max_per_caller", limits.get("per_caller", MAX_CALLER))
        total = limits.get("total", MAX_STREAMS)
        # Briefly queue for a slot: concurrent agents and forks routinely overlap.
        for attempt in range(SLOT_WAIT * 10 + 1):
            provider = self.providers.get(gid, {}).get("conn")
            if provider is None:
                raise Denied("eligible API provider is not online")
            if (len(self.streams) < total and
                    sum(s.caller is conn for s in self.streams.values()) < per_caller and
                    sum(s.provider is provider for s in self.streams.values()) < limit):
                break
            if attempt == SLOT_WAIT * 10:
                raise ValueError("API capacity exhausted")
            await asyncio.sleep(.1)
        sid = secrets.token_hex(16)
        s = self.streams[sid] = Stream(sid, conn, provider, body["model"], body.get("service_tier", "default"), protocol)
        s.timer = asyncio.create_task(self.expire(s))
        if not await provider.send({"type": "api_request", "version": API_VERSION, "id": sid, "body": body,
                                    "protocol": protocol, "beta": beta}):
            await self.finish(s, "provider_disconnected")
        return {"id": sid, "provider": gid, "backend": backend,
                "requested_model": s.model, "requested_tier": s.tier}

    async def expire(self, s):
        await asyncio.sleep(STREAM_TTL)
        if self.streams.get(s.id) is s:
            await self.finish(s, "timeout")
            # A vanished consumer must not retain a stream slot indefinitely.
            await asyncio.sleep(IDLE_TIMEOUT)
            self.remove(s)

    def remove(self, s):
        self.streams.pop(s.id, None)
        if s.timer and s.timer is not asyncio.current_task():
            s.timer.cancel()

    async def finish(self, s, error):
        if s.ended:
            return
        s.ended = True
        while not s.queue.empty():
            s.queue.get_nowait()
        s.queue.put_nowait({"done": True, "error": error})
        await s.provider.send({"type": "api_cancel", "id": s.id})

    async def push(self, conn, p):
        s = self.streams.get(p.get("id"))
        if s is None or s.provider is not conn or self.temple.galaxies.get(conn.id) is not conn:
            raise Denied("unknown provider stream")
        if s.ended or s.pushing or p.get("seq") != s.seq:
            raise ValueError("invalid provider stream sequence")
        error = p.get("error")
        if error:
            await self.finish(s, error if error in ERRORS else "provider_error")
            return True
        data = p.get("data", "")
        if not isinstance(data, str) or len(data) > 4 * ((MAX_CHUNK + 2) // 3):
            await self.finish(s, "too_large")
            raise ValueError("API chunk too large")
        try:
            decoded = base64.b64decode(data, validate=True)
        except ValueError:
            raise ValueError("invalid API chunk") from None
        s.total += len(decoded)
        if len(decoded) > MAX_CHUNK or s.total > MAX_STREAM:
            await self.finish(s, "too_large")
            raise ValueError("API stream too large")
        meta = p.get("meta") or {}
        meta = {k: str(meta[k])[:128] for k in ("returned_model", "returned_tier") if k in meta}
        s.pushing = True
        try:
            await asyncio.wait_for(s.queue.put({"data": data, "done": bool(p.get("done")), "meta": meta}), IDLE_TIMEOUT)
            s.seq += 1
            if p.get("done"):
                s.ended = True
        except asyncio.TimeoutError:
            await self.finish(s, "backpressure")
        finally:
            s.pushing = False
        return True

    async def next(self, conn, p):
        self.consumer(conn)
        s = self.streams.get(p.get("id"))
        if s is None or s.caller is not conn:
            raise Denied("unknown caller stream")
        if s.reading:
            raise ValueError("only one reader per stream")
        s.reading = True
        try:
            try:
                part = await asyncio.wait_for(s.queue.get(), IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                await self.finish(s, "timeout")
                part = await s.queue.get()
            if part.get("done"):
                self.remove(s)
            return part
        finally:
            s.reading = False

    async def cancel(self, conn, p):
        s = self.streams.get(p.get("id"))
        if s is None:
            return True
        if s.caller is not conn:
            raise Denied("not this stream's caller")
        await self.finish(s, "cancelled")
        confirmed = False
        try:
            await asyncio.wait_for(s.cancel_ack.wait(), CANCEL_TIMEOUT)
            confirmed = True
        except asyncio.TimeoutError:
            pass
        self.remove(s)
        return {"cancelled": True, "upstream_closed": confirmed}

    def cancelled(self, conn, p):
        s = self.streams.get(p.get("id"))
        if s is None:
            return False
        if s.provider is not conn or self.temple.galaxies.get(conn.id) is not conn:
            raise Denied("unknown provider cancellation")
        s.cancel_ack.set()
        return True

    async def disconnect(self, conn):
        if self.providers.get(conn.id, {}).get("conn") is conn:
            self.providers.pop(conn.id, None)
        for s in list(self.streams.values()):
            if s.provider is conn:
                await self.finish(s, "provider_disconnected")
            elif s.caller is conn:
                await self.finish(s, "caller_disconnected")
                self.remove(s)


class ProviderWorker:
    """Outbound-only gateway worker. No listening socket and no remote config API."""
    def __init__(self, link, config: dict):
        self.link, self.config = link, config
        self.tasks: dict[str, asyncio.Task] = {}
        self.generation = 0

    async def register(self, revision: str):
        backends = {self.config.get("model_backends", {}).get(m, "codex") for m in self.config.get("models", [])}
        if "codex" in backends:
            self.local_transport()
        if "claude" in backends:
            self.local_transport("messages")
        self.ssl_context()
        models = self.config.get("models", [])
        await self.link.call("api_register", version=API_VERSION, protocol="responses",
                             models=models, model_backends=self.config.get("model_backends", {}),
                             revision=revision, timeout=15)

    async def disconnect(self):
        self.generation += 1
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    async def frame(self, frame):
        sid = frame.get("id")
        if frame["type"] == "api_cancel":
            task = self.tasks.get(sid)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            try:
                await self.link.call("api_cancel_ack", id=sid, timeout=5)
            except Exception:
                pass
            return
        if frame.get("version") != API_VERSION or not isinstance(sid, str) or len(sid) != 32:
            return
        if sid in self.tasks:
            return
        if len(self.tasks) >= self.config.get("max_streams", MAX_PROVIDER):
            await self.link.call("api_push", id=sid, seq=0, error="backpressure", timeout=10)
            return
        task = asyncio.create_task(self.run(sid, frame.get("body"), self.generation,
                                            frame.get("protocol", "responses"), frame.get("beta", "")))
        self.tasks[sid] = task
        task.add_done_callback(lambda _: self.tasks.pop(sid, None))

    def ssl_context(self):
        # Explicit provider-local CA source also works in Conda daemons with an empty default store.
        context = ssl.create_default_context(cafile=self.config.get("ca_file", "/etc/ssl/certs/ca-certificates.crt"))
        if not context.get_ca_certs():
            raise ValueError("provider CA store is empty")
        return context

    def local_transport(self, protocol="responses"):
        cfg = self.config
        if protocol == "messages":
            path = Path(cfg["claude_config_file"])
            if not path.is_absolute() or path.stat().st_mode & 0o077:
                raise ValueError("provider config file must be private")
            local = json.loads(path.read_text()).get("env", {})
            base_url = local.get("ANTHROPIC_BASE_URL", "")
            key = local.get("ANTHROPIC_AUTH_TOKEN") or local.get("ANTHROPIC_API_KEY", "")
        elif cfg.get("codex_config_file"):
            # Read existing provider-local configuration without exporting it.
            path = Path(cfg["codex_config_file"])
            if not path.is_absolute() or path.stat().st_mode & 0o077:
                raise ValueError("provider config file must be private")
            local = tomllib.loads(path.read_text())
            name = cfg.get("codex_provider") or local.get("model_provider")
            provider = local.get("model_providers", {}).get(name, {})
            base_url = provider.get("base_url", "")
            if provider.get("wire_api", "responses") != "responses" or provider.get("requires_openai_auth"):
                raise ValueError("provider must support Responses with local bearer authentication")
            key = provider.get("experimental_bearer_token") or os.environ.get(provider.get("env_key", ""), "")
        else:
            base_url = cfg.get("base_url", "")
            path = Path(cfg["api_key_file"])
            if not path.is_absolute() or path.stat().st_mode & 0o077:
                raise ValueError("provider key file must be private")
            key = path.read_text().strip()
        url = urlsplit(base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("invalid provider URL")
        if not key or not isinstance(key, str) or any(c.isspace() for c in key):
            raise ValueError("invalid provider key")
        suffix = "/v1/messages" if protocol == "messages" else "/responses"
        return base_url.rstrip("/") + suffix, key

    async def run(self, sid, raw_body, generation, protocol="responses", beta=""):
        try:
            import httpx
        except ImportError:
            import httpx2 as httpx  # Existing harness environments may ship this compatible distribution.
        seq = 0

        async def push(**part):
            nonlocal seq
            if generation != self.generation:
                raise asyncio.CancelledError
            await self.link.call("api_push", id=sid, seq=seq, timeout=IDLE_TIMEOUT + 5, **part)
            seq += 1

        try:
            body = request_body(raw_body, self.config["models"], protocol)
            backend = self.config.get("model_backends", {}).get(body["model"], "codex")
            if protocol != ("messages" if backend == "claude" else "responses"):
                raise ValueError("model requires its native backend protocol")
            url, key = self.local_transport("messages") if protocol == "messages" else self.local_transport()
            headers = {"Authorization": f"Bearer {key}", "Accept": "text/event-stream"}
            if protocol == "messages":
                headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
                if beta and BETA.fullmatch(beta):
                    headers["anthropic-beta"] = beta
            # Provider-local proxy only; never sent to temple or accepted from a caller.
            async with httpx.AsyncClient(proxy=self.config.get("proxy"), trust_env=False, verify=self.ssl_context(),
                                         follow_redirects=False, timeout=httpx.Timeout(IDLE_TIMEOUT, connect=15)) as client:
                async with client.stream("POST", url, json=body,
                                         headers=headers) as response:
                    if response.status_code != 200:
                        code = ("upstream_auth" if response.status_code in (401, 403) else
                                "upstream_rate_limit" if response.status_code == 429 else
                                "upstream_rejected" if response.status_code < 500 else "upstream_unavailable")
                        await push(error=code)
                        return
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        await push(error="invalid_stream")
                        return
                    pending = b""
                    complete = False
                    message_started = False
                    message_stopped = False
                    async for chunk in response.aiter_bytes():
                        pending += chunk
                        if len(pending) > MAX_EVENT:
                            await push(error="too_large")
                            return
                        # SSE allows CRLF as well as LF.
                        pending = pending.replace(b"\r\n", b"\n")
                        while b"\n\n" in pending:
                            block, pending = pending.split(b"\n\n", 1)
                            data = b"\n".join(line[5:].lstrip() for line in block.split(b"\n") if line.startswith(b"data:"))
                            if not data or data == b"[DONE]":
                                continue
                            event = json.loads(data)
                            kind = event.get("type", "")
                            if kind in ("error", "response.failed", "response.incomplete"):
                                category = (event.get("error") or {}).get("type")
                                code = {"authentication_error": "upstream_auth", "permission_error": "upstream_auth",
                                        "rate_limit_error": "upstream_rate_limit", "overloaded_error": "upstream_unavailable",
                                        "api_error": "upstream_unavailable"}.get(category, "upstream_rejected")
                                await push(error=code)
                                return
                            # Credential redaction is defense in depth against an upstream echo.
                            encoded = json.dumps(event, ensure_ascii=False).replace(key, "[redacted]").encode()
                            wire = (("event: " + kind + "\n").encode() if protocol == "messages" else b"") + b"data: " + encoded + b"\n\n"
                            meta = {}
                            if kind == "response.completed":
                                terminal = event.get("response", {})
                                meta = {k: terminal.get(source) or "unknown"
                                        for k, source in (("returned_model", "model"), ("returned_tier", "service_tier"))}
                            elif protocol == "messages" and kind == "message_start":
                                message_started = True
                                meta = {"returned_model": event.get("message", {}).get("model") or "unknown",
                                        "returned_tier": "unknown"}
                            elif protocol == "messages" and kind == "message_delta":
                                message_stopped = bool(event.get("delta", {}).get("stop_reason"))
                            for offset in range(0, len(wire), MAX_CHUNK):
                                await push(data=base64.b64encode(wire[offset:offset + MAX_CHUNK]).decode(), meta=meta)
                            if kind == "response.completed":
                                if event.get("response", {}).get("status") != "completed":
                                    await push(error="invalid_stream")
                                    return
                                complete = True
                                break
                            if protocol == "messages" and kind == "message_stop":
                                complete = message_started and message_stopped
                                break
                        if complete:
                            break
                    await push(done=True) if complete else await push(error="invalid_stream")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never send exception text, raw HTTP bodies, headers or local config.
            try:
                await push(error="provider_error")
            except Exception:
                pass
