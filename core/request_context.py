"""
Per-request context carriers that must be importable by both the logger
and the middleware layer. Kept in core/ to avoid circular imports.
"""
from __future__ import annotations

import contextvars

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "prism_request_id", default=None
)


def current_request_id() -> str | None:
    return request_id_var.get()
