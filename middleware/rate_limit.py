from __future__ import annotations
import math
import time
from collections import defaultdict
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)
from fastmcp.server.middleware.rate_limiting import (
    RateLimitError,
    SlidingWindowRateLimiter,
    TokenBucketRateLimiter,
)

from core.logger import get_logger

logger = get_logger("prism.middleware.rate_limit")

EXPENSIVE_TOOLS = {"review_pr", "comment_on_pr", "assign_reviewer"}


def _bucket_retry_after(bucket: TokenBucketRateLimiter, tokens: int = 1) -> float:
    deficit = max(0.0, tokens - bucket.tokens)
    if bucket.refill_rate <= 0:
        return float("inf")
    return math.ceil(deficit / bucket.refill_rate)


def _window_retry_after(win: SlidingWindowRateLimiter) -> float:
    if not win.requests:
        return 0.0
    oldest = win.requests[0]
    return max(0.0, math.ceil(oldest + win.window_seconds - time.time()))


class RateLimitMiddleware(Middleware):
    """
    Three-layer guardrail against runaway agent loops, noisy tenants,
    and LLM cost blow-ups. Runs AFTER AuthMiddleware so the key is the
    authenticated GitHub user, not a shared session id.

    Layers (checked in order):
      1. Strict sliding window for expensive/LLM tools (review_pr, ...).
      2. Per-user-per-tool token bucket (one tool can't starve the others).
      3. Per-user global token bucket (overall session budget).

    On breach: raises RateLimitError with a `retry_after` hint.
    On success: attaches current quota to ctx state under `rate_limit_quota`
    so tools / clients can self-throttle.
    """

    def __init__(
        self,
        session_req_per_sec: float = 0.5,
        session_burst: int = 30,
        tool_req_per_sec: float = 2.0,
        tool_burst: int = 10,
        expensive_max_req: int = 5,
        expensive_window_sec: int = 60,
    ):
        self.session_limiters: dict[str, TokenBucketRateLimiter] = defaultdict(
            lambda: TokenBucketRateLimiter(
                capacity=session_burst, refill_rate=session_req_per_sec
            )
        )
        self.tool_limiters: dict[str, TokenBucketRateLimiter] = defaultdict(
            lambda: TokenBucketRateLimiter(
                capacity=tool_burst, refill_rate=tool_req_per_sec
            )
        )
        self.expensive_limiters: dict[str, SlidingWindowRateLimiter] = defaultdict(
            lambda: SlidingWindowRateLimiter(
                max_requests=expensive_max_req, window_seconds=expensive_window_sec
            )
        )

    async def _identify(self, context: MiddlewareContext) -> str:
        fmc = context.fastmcp_context
        if fmc is not None:
            user = await fmc.get_state("github_user")
            if user:
                return user
            session_id = getattr(fmc, "session_id", None)
            if session_id:
                return session_id
        return "anonymous"

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext,
    ) -> Any:
        user = await self._identify(context)
        tool_name = context.message.name

        if tool_name in EXPENSIVE_TOOLS:
            expensive = self.expensive_limiters[user]
            if not await expensive.is_allowed():
                retry_after = _window_retry_after(expensive)
                logger.warning(
                    "Rate limit (expensive) hit user=%s tool=%s retry_after=%ss",
                    user, tool_name, retry_after,
                )
                raise RateLimitError(
                    f"Rate limit exceeded for `{tool_name}`. "
                    f"Retry after {retry_after}s. "
                    "This tool is capped to prevent runaway cost."
                )

        tool_key = f"{user}:{tool_name}"
        tool_bucket = self.tool_limiters[tool_key]
        if not await tool_bucket.consume():
            retry_after = _bucket_retry_after(tool_bucket)
            logger.warning(
                "Rate limit (per-tool) hit user=%s tool=%s retry_after=%ss",
                user, tool_name, retry_after,
            )
            raise RateLimitError(
                f"Rate limit exceeded for tool: {tool_name}. Retry after {retry_after}s."
            )

        session_bucket = self.session_limiters[user]
        if not await session_bucket.consume():
            retry_after = _bucket_retry_after(session_bucket)
            logger.warning(
                "Rate limit (session) hit user=%s retry_after=%ss", user, retry_after
            )
            raise RateLimitError(
                f"Global session rate limit exceeded for user {user}. "
                f"Retry after {retry_after}s."
            )

        if context.fastmcp_context is not None:
            quota = {
                "tool": tool_name,
                "tool_remaining": int(tool_bucket.tokens),
                "tool_capacity": tool_bucket.capacity,
                "session_remaining": int(session_bucket.tokens),
                "session_capacity": session_bucket.capacity,
            }
            if tool_name in EXPENSIVE_TOOLS:
                exp = self.expensive_limiters[user]
                quota["expensive_remaining"] = exp.max_requests - len(exp.requests)
                quota["expensive_capacity"] = exp.max_requests
            await context.fastmcp_context.set_state("rate_limit_quota", quota)

        return await call_next(context)
