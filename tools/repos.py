from __future__ import annotations
from fastmcp import FastMCP, Context
from core.logger import get_logger
from tools.auth import get_client  # import the shared helper

logger = get_logger("prism.tools.repos")

repos_server = FastMCP("repos")

@repos_server.tool
async def list_repos(
    ctx: Context,
    sort: str = "updated",
    repo_type: str = "all",
    limit: int = 30,
) -> list[dict]:
    """
    Lists all GitHub repositories accessible to the authenticated user.
    Call this when the user asks about their repos, projects, or codebases.
    """
    client = await get_client(ctx)  # handles auth + .env fallback

    limit = max(1, min(limit, 300))
    max_pages = (limit // 100) + 1
    logger.info("list_repos(sort=%s, type=%s, limit=%d)", sort, repo_type, limit)

    raw_repos = await client.get_repos(
        sort=sort,
        repo_type=repo_type,
        max_pages=max_pages,
        per_page=min(limit, 100),
    )
    raw_repos = raw_repos[:limit]

    repos = [
        {
            "name": r["name"],
            "full_name": r["full_name"],
            "description": r.get("description") or "",
            "language": r.get("language") or "Unknown",
            "stars": r["stargazers_count"],
            "forks": r["forks_count"],
            "open_issues": r["open_issues_count"],
            "visibility": r.get("visibility", "unknown"),
            "default_branch": r["default_branch"],
            "html_url": r["html_url"],
            "updated_at": r["updated_at"],
        }
        for r in raw_repos
    ]
    logger.info("Returning %d repos", len(repos))
    return repos