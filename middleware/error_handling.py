from __future__ import annotations
import traceback
from typing import Any

from mcp import McpError
from mcp.types import ErrorData
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from core.logger import get_logger
from core.request_context import current_request_id

logger = get_logger("prism.middleware.error")


class ValidationError(ToolError):
    """Raised when a tool's arguments fail validation. Maps to JSON-RPC -32602."""


# JSON-RPC 2.0 / MCP-aligned error codes. Kept as module constants so swapping
# them in one place is cheap if the MCP spec evolves.
_AUTH_CODE = -32001
_VALIDATION_CODE = -32602
_SERVER_ERROR = -32000
_INTERNAL_ERROR = -32603


def _with_rid(message: str) -> str:
    rid = current_request_id()
    return f"{message} [request_id={rid}]" if rid else message


def _classify(exc: Exception) -> int:
    name = type(exc).__name__.lower()
    if "auth" in name:
        return _AUTH_CODE
    if "ratelimit" in name:
        return _SERVER_ERROR
    if "validation" in name or "invalid" in name:
        return _VALIDATION_CODE
    return _SERVER_ERROR


class ErrorHandlingMiddleware(Middleware):
    """
    Single funnel for every error a tool can raise.

    - Already-shaped MCP errors (RateLimitError, anything subclassing McpError)
      pass through untouched — they already carry a code and a clean message.
    - Known ToolError subclasses (AuthenticationError, ValidationError, ...)
      are mapped to a sensible JSON-RPC code and the exception's own message
      is surfaced to the client.
    - Bad params (ValueError, TypeError) become -32602 invalid params.
    - Anything else is logged server-side with a full stack trace and a
      generic "Internal server error." is returned — keeping file paths,
      library versions, and stray secrets out of the orchestrator's view.

    Runs outer to everything except RequestIDMiddleware so every error is
    caught and every error is stamped with the request_id.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        tool = getattr(context.message, "name", "unknown")
        try:
            return await call_next(context)
        except McpError:
            # Already carries code + clean message (e.g. RateLimitError).
            raise
        except ToolError as exc:
            code = _classify(exc)
            logger.warning(
                "Handled tool error tool=%s type=%s code=%s msg=%s",
                tool, type(exc).__name__, code, exc,
            )
            raise McpError(
                ErrorData(code=code, message=_with_rid(str(exc)))
            ) from exc
        except (ValueError, TypeError) as exc:
            logger.warning(
                "Bad params tool=%s type=%s msg=%s",
                tool, type(exc).__name__, exc,
            )
            raise McpError(
                ErrorData(
                    code=_VALIDATION_CODE,
                    message=_with_rid(f"Invalid params: {exc}"),
                )
            ) from exc
        except Exception as exc:
            logger.error(
                "Unhandled exception tool=%s type=%s: %s\n%s",
                tool, type(exc).__name__, exc, traceback.format_exc(),
            )
            raise McpError(
                ErrorData(
                    code=_INTERNAL_ERROR,
                    message=_with_rid("Internal server error."),
                )
            ) from exc
