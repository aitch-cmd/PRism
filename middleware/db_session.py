from __future__ import annotations
from typing import Any

from fastmcp import Context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.logger import get_logger

logger = get_logger("prism.middleware.db_session")


class DatabaseSessionMiddleware(Middleware):
    """
    Per-tool unit-of-work. Opens a SQLAlchemy async session on entry, publishes
    it to ctx state as `db_session`, and on exit either commits (success) or
    rolls back (exception). The session is always closed.

    If DATABASE_URL isn't configured (get_db() returns None), this middleware
    is a no-op — stateless tools keep working without a database.

    Runs innermost so cached idempotency hits never touch the pool.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        db = get_db()
        fmc = context.fastmcp_context
        if db is None or fmc is None:
            return await call_next(context)

        session: AsyncSession = db.session()
        await fmc.set_state("db_session", session, serializable=False)

        try:
            result = await call_next(context)
        except Exception:
            await session.rollback()
            logger.debug("Transaction rolled back")
            raise
        else:
            await session.commit()
            logger.debug("Transaction committed")
            return result
        finally:
            await session.close()
            # Drop the reference so state read after the call fails loudly
            # instead of silently handing back a closed session.
            await fmc.set_state("db_session", None, serializable=False)


async def get_session(ctx: Context) -> AsyncSession | None:
    """
    Accessor for tools: returns the AsyncSession that DatabaseSessionMiddleware
    attached to the request context. Returns None when DATABASE_URL isn't
    configured — callers that need persistence should guard on that.
    """
    return await ctx.get_state("db_session")
