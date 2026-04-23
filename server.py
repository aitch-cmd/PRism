from __future__ import annotations
import os
from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
from dotenv import load_dotenv
from core.logger import get_logger
from middleware import AuthMiddleware, RateLimitMiddleware
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
    try:
        yield {} 
    finally:
        logger.info("PRism shutting down")

mcp = FastMCP(
    "PRism",
    lifespan=prism_lifespan,
    instructions=(
        "PRism lets you talk to your GitHub account in plain English. "
        "List repos, browse issues, review PRs — all without leaving chat."
    ),
)

mcp.add_middleware(AuthMiddleware())
mcp.add_middleware(RateLimitMiddleware())

mcp.mount(repos_server)
mcp.mount(issues_server)
mcp.mount(prs_server)
mcp.mount(dashboard_server)
mcp.mount(user_server)

if __name__ == "__main__":
    mcp.run(transport="http", port=8000)