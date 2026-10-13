"""Runtime selection: which backend/model an agent runs on, with fallback.

A role's runtime config (from galaxy config defaults, overridden by temple
settings the owner edits in the web UI):

  {"backend": "claude" | "codex", "model": ..., "effort": ...,
   "memory_model": ..., "memory_effort": ...,
   "fallback": {"backend": ..., "model": ..., "effort": ...} | null}

Threads are backend-specific and cannot move between Codex and Claude. When
the active backend changes, the agent opens a new thread and the temple's
backend-neutral turn log bridges the gap (see Agent.handoff_context).
"""
from __future__ import annotations

import time
import asyncio

from .runtime import CodexRuntime, TurnResult, RuntimeError_

RETRY_DELAYS = (0.5,)  # One retry AFTER the first attempt, on each source.


def recoverable(error: str | None) -> bool:
    error = (error or '').lower()
    if any(s in error for s in ('cancelled', 'canceled', 'unauthorized', 'forbidden', 'upstream_auth', '401', '403',
                               'invalid api key', 'invalid argument', 'permission denied',
                               'context length', 'too_large', 'not found', 'unsupported')):
        return False
    return any(s in error for s in ('invalid_stream', 'timeout', 'timed out', 'disconnected',
        'connection', 'transport', 'stream', 'temporar', 'overloaded', 'rate limit', '429',
        '408', '500', '502', '503', '504', '529', 'backpressure', 'econnreset', 'offline', 'unavailable', 'app-server exited',
        'app-server is not running', 'session ended without a result', 'provider_error',
        'workspace routing discovery failed', 'upstream_rate_limit'))

# Codex models whose default reasoning effort the harness pins explicitly.
DEFAULT_EFFORT = {"gpt-6-astra": "medium"}

# After the primary fails, stay on the fallback this long before trying it again.
FALLBACK_HOLD = 30 * 60


def build(cfg: dict, *, codex: str, cwd: str, log_dir, sub_agents: bool, api_link=None):
    backend = cfg.get("backend", "codex")
    if backend not in ("codex", "galaxy_api", "claude"):
        raise ValueError(f"unknown backend {backend}")
    if backend == "claude":
        from .claude_runtime import ClaudeRuntime
        transport = None
        if cfg.get("provider"):
            from .galaxy_api_runtime import GalaxyAPITransport
            transport = GalaxyAPITransport(link=api_link, provider=cfg["provider"], state_dir=log_dir,
                                           model=cfg["model"], protocol="messages")
        return ClaudeRuntime(cwd, model=cfg.get("model") or "claude-opus-5-5", effort=cfg.get("effort"),
                             log_path=str(log_dir / "claude.log"), cli_path=cfg.get("cli_path"),
                             api_transport=transport)
    if backend == "galaxy_api":
        if not isinstance(cfg.get("provider"), str) or not cfg["provider"] or not cfg.get("model"):
            raise ValueError("galaxy_api requires an explicit provider and model")
        from .galaxy_api_runtime import GalaxyAPIRuntime
        return GalaxyAPIRuntime(codex, cwd, link=api_link, provider=cfg.get("provider"),
                                state_dir=log_dir, model=cfg.get("model"),
                                effort=cfg.get("effort") or DEFAULT_EFFORT.get(cfg.get("model")),
                                service_tier=cfg.get("service_tier"), sub_agents=sub_agents)
    return CodexRuntime(codex, cwd, model=cfg.get("model"), effort=cfg.get("effort") or DEFAULT_EFFORT.get(cfg.get("model")),
                        sub_agents=sub_agents,
                        log_path=str(log_dir / f"{backend}.log"),
                        service_tier=cfg.get("service_tier"))


def label(cfg: dict | None) -> str:
    if not cfg:
        return ""
    if cfg.get("provider"):
        return f"{cfg.get('backend')}:{cfg.get('provider')}:{cfg.get('model')}"
    return f"{cfg.get('backend', 'codex')}:{cfg.get('model') or 'default'}"


def same(a: dict | None, b: dict | None) -> bool:
    keys = ("backend", "model", "effort", "service_tier", "provider")
    if a is None or b is None:
        return a is b
    return bool(a and b) and all((a or {}).get(k) == (b or {}).get(k) for k in keys)


class Backends:
    """Primary and optional fallback runtime for one agent."""

    def __init__(self, cfg: dict, **build_kw):
        self.build_kw = build_kw
        self.cfg = cfg
        self.primary = build(cfg, **build_kw)
        fb = cfg.get("fallback")
        self.fallback = build(fb, **build_kw) if fb else None
        self.failed_at = 0.0
        self.hold = FALLBACK_HOLD
        self.started = False

    @property
    def using_fallback(self) -> bool:
        selected = getattr(self, '_selected', None)
        if selected is not None:
            return selected[0] is self.fallback
        return self.fallback is not None and time.time() - self.failed_at < self.hold

    @property
    def active(self):
        selected = getattr(self, '_selected', None)
        if selected is not None:
            return selected[0]
        return self.fallback if self.using_fallback else self.primary

    @property
    def active_cfg(self) -> dict:
        selected = getattr(self, '_selected', None)
        if selected is not None:
            return selected[1]
        return self.cfg["fallback"] if self.using_fallback else self.cfg

    def trip(self, hold: float = FALLBACK_HOLD) -> bool:
        """Primary failed: activate fallback for hold seconds from this switch."""
        if self.fallback is None or self.using_fallback:
            return False
        self.failed_at, self.hold = time.time(), hold
        return True

    async def start(self) -> None:
        if self.started:
            return
        async def attempt(rt, note):
            await rt.start()
            return TurnResult('completed')
        result = await self.recover(attempt)
        if result.status != 'completed':
            raise RuntimeError_(result.error or result.status)
        self.started = True

    async def recover(self, attempt, *, prepare=None, report=None, cancel=None):
        """One serial recovery policy for startup, foreground, patrol and scoped turns.

        prepare must settle the previous execution and return checked context before
        another model call. It is never a replay of tool calls or message ingress.
        """
        cancel = cancel or asyncio.Event()
        rt = self.active
        cfg = self.active_cfg
        choices = [(rt, cfg)]
        if rt is self.primary and self.fallback is not None:
            choices.append((self.fallback, self.cfg['fallback']))
        note, total = '', 0
        state = {}
        async def publish(phase, **kw):
            state.update(phase=phase, **kw)
            if report:
                await report(dict(state))
        try:
            for source, (rt, cfg) in enumerate(choices):
                if source:
                    self.trip()
                self._selected = (rt, cfg)
                for retry in range(len(RETRY_DELAYS) + 1):
                    if cancel.is_set():
                        await publish('interrupted')
                        return TurnResult('interrupted', error='Recovery cancelled')
                    total += 1
                    await publish('running', attempt=total, retry=retry, retries=len(RETRY_DELAYS),
                                  source=label(cfg), fallback=rt is self.fallback)
                    if cancel.is_set():
                        await publish('interrupted')
                        return TurnResult('interrupted', error='Recovery cancelled')
                    try:
                        result = await attempt(rt, note)
                    except asyncio.CancelledError:
                        await publish('interrupted')
                        raise
                    except Exception as e:
                        result = TurnResult('failed', error=f'{type(e).__name__}: {e}')
                    if cancel.is_set():
                        result = TurnResult('interrupted', error='Recovery cancelled')
                    if result.status != 'failed' or not recoverable(result.error):
                        await publish(result.status, error=result.error)
                        return result
                    await publish('checking', error=result.error)
                    if prepare:
                        try:
                            note = await prepare(rt, result, note)
                        except Exception as e:
                            result.error = f'{result.error}; recovery cannot safely continue: {e}'
                            await publish('failed', error=result.error)
                            return result
                    if retry < len(RETRY_DELAYS):
                        await publish('retrying')
                        try:
                            await asyncio.wait_for(cancel.wait(), RETRY_DELAYS[retry])
                        except asyncio.TimeoutError:
                            pass
                    elif source + 1 < len(choices):
                        await publish('switching')
            result.error = (result.error or 'Backend failure') + ('; configured fallback exhausted' if len(choices) > 1
                            or rt is self.fallback else '; no configured fallback is available')
            await publish('failed', error=result.error)
            return result
        finally:
            self._selected = None

    async def close(self) -> None:
        for rt in (self.primary, self.fallback):
            if rt:
                await rt.close()
