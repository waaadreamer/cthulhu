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

from .runtime import CodexRuntime

# After the primary fails, stay on the fallback this long before trying it again.
FALLBACK_HOLD = 30 * 60


def build(cfg: dict, *, codex: str, cwd: str, log_dir, sub_agents: bool):
    if cfg.get("backend") == "claude":
        from .claude_runtime import ClaudeRuntime
        return ClaudeRuntime(cwd, model=cfg.get("model") or "claude-opus-5-5", effort=cfg.get("effort"),
                             log_path=str(log_dir / "claude.log"), cli_path=cfg.get("cli_path"))
    return CodexRuntime(codex, cwd, model=cfg.get("model"), effort=cfg.get("effort"), sub_agents=sub_agents,
                        log_path=str(log_dir / "codex.log"))


def label(cfg: dict | None) -> str:
    if not cfg:
        return ""
    return f"{cfg.get('backend', 'codex')}:{cfg.get('model') or 'default'}"


def same(a: dict | None, b: dict | None) -> bool:
    keys = ("backend", "model", "effort")
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
        self.started = True
        await self.primary.start()
        if self.fallback:
            await self.fallback.start()

    async def close(self) -> None:
        for rt in (self.primary, self.fallback):
            if rt:
                await rt.close()
