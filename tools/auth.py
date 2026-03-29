from __future__ import annotations
import os
from fastmcp import FastMCP, Context
from github_client import GitHubClient, GitHubClientError
from logger import get_logger

logger = get_logger("prism.tools.auth")

auth_server = FastMCP("auth")


async def get_client(ctx: Context) -> GitHubClient:
    """
    Shared helper — called by every tool.
    1. Returns a client from the token stored in session state.
    2. Falls back to GITHUB_PAT in .env for local dev.
    3. Raises clearly if neither exists.
    """
    # 1. Check session state for a token (set by authenticate tool)
    token = await ctx.get_state("github_token")

    # 2. Fall back to .env
    if not token:
        token = os.getenv("GITHUB_PAT")

    if not token:
        raise ValueError(
            "🔒 Not authenticated. Call `authenticate` with your GitHub PAT "
            "or set GITHUB_PAT in .env."
        )

    # Create a fresh client each call (token is serializable, client isn't)
    client = GitHubClient(token)
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
    finally:
        await client.close()

    # Store only serializable values — token is a string
    await ctx.set_state("github_token", token)
    await ctx.set_state("github_user", user["login"])

    logger.info("Authenticated as %s", user["login"])
    return f"✅ Authenticated as **{user['login']}**"