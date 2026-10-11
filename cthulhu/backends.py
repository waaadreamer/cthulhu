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
import logging

from .runtime import CodexRuntime

# Codex models whose default reasoning effort the harness pins explicitly.
DEFAULT_EFFORT = {"gpt-6-astra": "medium"}

# After the primary fails, stay on the fallback this long before trying it again.
FALLBACK_HOLD = 30 * 60


def build(cfg: dict, *, codex: str, cwd: str, log_dir, sub_agents: bool, company_codex: dict | None = None):
    backend = cfg.get("backend", "codex")
    if backend not in ("codex", "company_codex", "claude"):
        raise ValueError(f"unknown backend {backend}")
    if backend == "claude":
        from .claude_runtime import ClaudeRuntime
        return ClaudeRuntime(cwd, model=cfg.get("model") or "claude-opus-5-5", effort=cfg.get("effort"),
                             log_path=str(log_dir / "claude.log"), cli_path=cfg.get("cli_path"))
    return CodexRuntime(codex, cwd, model=cfg.get("model"), effort=cfg.get("effort") or DEFAULT_EFFORT.get(cfg.get("model")),
                        sub_agents=sub_agents,
                        log_path=str(log_dir / f"{backend}.log"),
                        company=(company_codex or {}) if backend == "company_codex" else None,
                        service_tier=cfg.get("service_tier"))


def label(cfg: dict | None) -> str:
    if not cfg:
        return ""
    return f"{cfg.get('backend', 'codex')}:{cfg.get('model') or 'default'}"


def same(a: dict | None, b: dict | None) -> bool:
    keys = ("backend", "model", "effort", "service_tier")
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
        self.started = False

    @property
    def using_fallback(self) -> bool:
        return self.fallback is not None and time.time() - self.failed_at < FALLBACK_HOLD

    @property
    def active(self):
        return self.fallback if self.using_fallback else self.primary

    @property
    def active_cfg(self) -> dict:
        return self.cfg["fallback"] if self.using_fallback else self.cfg

    def trip(self) -> bool:
        """Primary failed: switch to the fallback if there is one."""
        if self.fallback is None or self.using_fallback:
            return False
        self.failed_at = time.time()
        return True

    async def start(self) -> None:
        if self.started:
            return
        try:
            await self.primary.start()
        except Exception:
            await self.primary.close()
            if not self.trip():
                raise
            logging.getLogger(__name__).warning("primary startup failed; starting configured fallback")
            await self.fallback.start()
        self.started = True

    async def close(self) -> None:
        for rt in (self.primary, self.fallback):
            if rt:
                await rt.close()
