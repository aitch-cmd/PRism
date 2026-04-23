from __future__ import annotations
import os
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext

from github_client import GitHubClient, GitHubClientError
from core.logger import get_logger

logger = get_logger("prism.middleware.auth")


class AuthenticationError(ToolError):
    """Raised when the caller cannot be authenticated."""


def _extract_token() -> str | None:
    headers = get_http_headers(include={"authorization"})
    auth = headers.get("authorization")
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
            return parts[1].strip()
        return auth.strip()

    token = headers.get("x-github-token")
    if token:
        return token.strip()

    return os.getenv("GH_PAT")


async def _resolve_identity(fastmcp_ctx: Context) -> GitHubClient:
    existing = await fastmcp_ctx.get_state("github_client")
    if existing:
        return existing

    token = await fastmcp_ctx.get_state("github_token") or _extract_token()
    if not token:
        raise AuthenticationError(
            "Not authenticated. Provide a GitHub token via the "
            "`Authorization: Bearer <token>` header or set GH_PAT."
        )

    client = GitHubClient(token)
    try:
        user = await client.get_authenticated_user()
    except GitHubClientError as exc:
        await client.close()
        logger.warning("Token validation failed: %s", exc)
        raise AuthenticationError(f"Invalid GitHub token: {exc}") from exc

    await fastmcp_ctx.set_state("github_token", token)
    await fastmcp_ctx.set_state("github_user", user["login"])
    await fastmcp_ctx.set_state("github_client", client, serializable=False)
    logger.info("Authenticated as %s", user["login"])
    return client


class AuthMiddleware(Middleware):
    """
    Authenticates every tool call and resource read by construction.

    Resolution order: Authorization header → X-GitHub-Token header → GH_PAT env.
    On success, attaches `github_client` and `github_user` to context state.
    On failure, short-circuits with a 401-style AuthenticationError — the
    downstream tool is never invoked.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        if context.fastmcp_context is not None:
            await _resolve_identity(context.fastmcp_context)
        return await call_next(context)

    async def on_read_resource(self, context: MiddlewareContext, call_next: CallNext):
        if context.fastmcp_context is not None:
            await _resolve_identity(context.fastmcp_context)
        return await call_next(context)


async def get_client(ctx: Context) -> GitHubClient:
    """
    Accessor for tools: returns the GitHubClient that AuthMiddleware
    attached to the request context. Never performs the auth dance itself.
    """
    client = await ctx.get_state("github_client")
    if client is None:
        raise AuthenticationError(
            "No authenticated client on context — AuthMiddleware did not run."
        )
    return client
