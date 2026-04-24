from __future__ import annotations
import os
from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
from dotenv import load_dotenv
from core.db import close_db, init_db
from core.logger import get_logger
from middleware import (
    AuthMiddleware,
    DatabaseSessionMiddleware,
    ErrorHandlingMiddleware,
    IdempotencyMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
)
from tools.repos import repos_server
from tools.issues import issues_server
from tools.prs import prs_server
from tools.dashboard import dashboard_server
from resources.user import user_server

load_dotenv()
logger = get_logger("prism.server")

@lifespan
async def prism_lifespan(server):
    logger.info("PRism starting up")
    await init_db()
    try:
        yield {}
    finally:
        await close_db()
        logger.info("PRism shutting down")

mcp = FastMCP(
    "PRism",
    lifespan=prism_lifespan,
    instructions=(
        "PRism lets you talk to your GitHub account in plain English. "
        "List repos, browse issues, review PRs — all without leaving chat."
    ),
)

# Order matters: added first = runs outermost. So requests flow in
# top-to-bottom and responses/exceptions unwind bottom-to-top.
#
#   RequestID      — stamp request_id first so every other layer's logs have it
#   ErrorHandling  — catch everything below and normalise the error shape
#   Auth           — resolve identity (user appears in all downstream logs/keys)
#   RateLimit      — keyed on the authenticated user
#   Idempotency    — collapse duplicates before we open a DB session
#   DatabaseSession — innermost: commit/rollback around the tool body
mcp.add_middleware(RequestIDMiddleware())
mcp.add_middleware(ErrorHandlingMiddleware())
mcp.add_middleware(AuthMiddleware())
mcp.add_middleware(RateLimitMiddleware())
mcp.add_middleware(IdempotencyMiddleware())
mcp.add_middleware(DatabaseSessionMiddleware())

mcp.mount(repos_server)
mcp.mount(issues_server)
mcp.mount(prs_server)
mcp.mount(dashboard_server)
mcp.mount(user_server)

if __name__ == "__main__":
    mcp.run(transport="http", port=8000)