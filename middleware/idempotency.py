from __future__ import annotations
import asyncio
import hashlib
import json
import time
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from core.logger import get_logger

logger = get_logger("prism.middleware.idempotency")

DEFAULT_TTL_SECONDS = 300

def _normalize(value: Any) -> Any:
    """Canonicalise args so `{pr:42}`, `{pr: 42}`, and `{pr:"42"}` all hash the same."""
    if isinstance(value, dict):
        return {k: _normalize(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lstrip("-").isdigit():
            try:
                return int(stripped)
            except ValueError:
                pass
        return stripped
    return value


def _compute_key(tool: str, args: dict[str, Any], user: str) -> str:
    payload = json.dumps(
        {"tool": tool, "args": _normalize(args or {}), "user": user},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class IdempotencyMiddleware(Middleware):
    """
    Collapses duplicate tool calls inside a short TTL window.

    Key = sha256(tool_name + normalized_args + user). On a hit, the cached
    result is returned without running the tool — critical for stateful tools
    like review_pr where a duplicate would insert a second row.

    Per-key locking also protects against a thundering herd: if ten identical
    calls arrive simultaneously, one executes and the rest wait on its result
    instead of racing.

    Bypass: pass `force=true` in the tool arguments.
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _read_fresh(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.monotonic():
            self._cache.pop(key, None)
            self._locks.pop(key, None)
            return None
        return value

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext,
    ) -> Any:
        tool = context.message.name
        args = dict(context.message.arguments or {})

        if args.pop("force", False) is True:
            logger.info("Bypass (force=true) tool=%s", tool)
            context.message.arguments = args or None
            return await call_next(context)

        fmc = context.fastmcp_context
        user = "anonymous"
        if fmc is not None:
            try:
                user = (await fmc.get_state("github_user")) or "anonymous"
            except Exception:
                user = "anonymous"

        key = _compute_key(tool, args, user)

        cached = self._read_fresh(key)
        if cached is not None:
            logger.info("Cache HIT tool=%s user=%s key=%s", tool, user, key[:10])
            return cached

        lock = self._get_lock(key)
        async with lock:
            # Second check: another coroutine may have populated the cache
            # while we were waiting for the lock.
            cached = self._read_fresh(key)
            if cached is not None:
                logger.info("Cache HIT (post-lock) tool=%s key=%s", tool, key[:10])
                return cached

            result = await call_next(context)
            self._cache[key] = (time.monotonic() + self.ttl, result)
            logger.info(
                "Cache STORE tool=%s user=%s key=%s ttl=%ss",
                tool, user, key[:10], self.ttl,
            )
            return result
