from __future__ import annotations
from fastmcp import FastMCP, Context
from core.logger import get_logger
from middleware.auth import get_client

logger = get_logger("prism.resources.user")

user_server = FastMCP("user")

@user_server.resource("github://user/profile")
async def user_profile(ctx: Context) -> dict:
    """Authenticated user's GitHub profile."""
    client = await get_client(ctx)
    user = await client.get_authenticated_user()
    return {
        "login": user["login"],
        "name": user.get("name"),
        "email": user.get("email"),
        "plan": user.get("plan", {}).get("name", "free"),
        "public_repos": user.get("public_repos", 0),
        "html_url": user.get("html_url"),
    }

@user_server.resource("github://user/orgs")
async def user_orgs(ctx: Context) -> list[dict]:
    """Organizations the authenticated user belongs to."""
    client = await get_client(ctx)
    orgs = await client.get_user_orgs()
    return [
        {"login": o["login"], "description": o.get("description") or ""}
        for o in orgs
    ]