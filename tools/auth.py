from __future__ import annotations
import os
from fastmcp import FastMCP, Context
from github_client import GitHubClient, GitHubClientError
from core.logger import get_logger

logger = get_logger("prism.tools.auth")

auth_server = FastMCP("auth")


async def get_client(ctx: Context) -> GitHubClient:
    """
    Shared helper — called by every tool.
    1. Returns existing client from state if already created this session.
    2. Creates one from session token or .env fallback.
    3. Raises clearly if neither exists.
    """
    # Return existing client if already created
    client = await ctx.get_state("github_client")
    if client:
        return client

    # Create from token
    token = await ctx.get_state("github_token") or os.getenv("GITHUB_PAT")
    if not token:
        raise ValueError(
            "🔒 Not authenticated. Call `authenticate` with your GitHub PAT "
            "or set GITHUB_PAT in .env."
        )

    client = GitHubClient(token)
    await ctx.set_state("github_client", client, serializable=False)
    return client


@auth_server.tool
async def authenticate(token: str, ctx: Context) -> str:
    """
    Authenticate with GitHub using a Personal Access Token (PAT).
    Call this when the user wants to connect their GitHub account
    or switch to a different token.
    Returns the authenticated username on success.
    """
    logger.info("authenticate() called — validating token")
    client = GitHubClient(token)
    try:
        user = await client.get_authenticated_user()
    except GitHubClientError as exc:
        await client.close()
        logger.warning("Token validation failed: %s", exc)
        return f"❌ Authentication failed: {exc}"

    # Store token (serializable) for cross-request persistence
    await ctx.set_state("github_token", token)
    await ctx.set_state("github_user", user["login"])
    # Store client (non-serializable) for reuse within this request
    await ctx.set_state("github_client", client, serializable=False)

    logger.info("Authenticated as %s", user["login"])
    return f"✅ Authenticated as **{user['login']}**"