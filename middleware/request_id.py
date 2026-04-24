from __future__ import annotations
import time
import uuid
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from core.logger import get_logger
from core.request_context import request_id_var

logger = get_logger("prism.middleware.request_id")


def _new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:10]}"


def _result_size(result: Any) -> int:
    try:
        return len(str(result))
    except Exception:
        return -1


class RequestIDMiddleware(Middleware):
    """
    First in the chain. Stamps every inbound tool call with a short unique id,
    publishes it via a contextvar so downstream async work — including the
    `[req-...]` marker on every log line — can see it, and emits one-line
    entry/exit records for the call.

    Pair this with core/logger.py's RequestIdFilter: once this middleware has
    set the contextvar, every `get_logger(...)` call anywhere in the request
    picks it up for free, no manual threading required.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext,
    ) -> Any:
        rid = _new_request_id()
        token = request_id_var.set(rid)

        fmc = context.fastmcp_context
        if fmc is not None:
            await fmc.set_state("request_id", rid)

        tool = context.message.name
        args = context.message.arguments or {}
        user: str | None = None
        if fmc is not None:
            try:
                user = await fmc.get_state("github_user")
            except Exception:
                user = None

        logger.info("ENTER tool=%s user=%s args=%s", tool, user or "-", args)
        start = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.info(
                "EXIT tool=%s status=error duration_ms=%s error_type=%s",
                tool, duration_ms, type(exc).__name__,
            )
            raise
        else:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.info(
                "EXIT tool=%s status=ok duration_ms=%s result_size=%s",
                tool, duration_ms, _result_size(result),
            )
            return result
        finally:
            request_id_var.reset(token)
