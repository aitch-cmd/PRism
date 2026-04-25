from __future__ import annotations
from fastmcp import FastMCP
from dotenv import load_dotenv
from core.logger import get_logger
from middleware import (
    AuthMiddleware,
    ErrorHandlingMiddleware,
    IdempotencyMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
)
from tools.repos import repos_server
from tools.issues import issues_server
from tools.prs import prs_server
from tools.dashboard import dashboard_server
from tools.team import team_server
from resources.user import user_server

load_dotenv()
logger = get_logger("prism.server")

mcp = FastMCP(
    "PRism",
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
#   Idempotency    — innermost: collapse duplicate requests
mcp.add_middleware(RequestIDMiddleware())
mcp.add_middleware(ErrorHandlingMiddleware())
mcp.add_middleware(AuthMiddleware())
mcp.add_middleware(RateLimitMiddleware())
mcp.add_middleware(IdempotencyMiddleware())

mcp.mount(repos_server)
mcp.mount(issues_server)
mcp.mount(prs_server)
mcp.mount(dashboard_server)
mcp.mount(team_server)
mcp.mount(user_server)

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)