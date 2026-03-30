from __future__ import annotations
from fastmcp import FastMCP, Context
from core.logger import get_logger
from tools.auth import get_client

logger = get_logger("prism.tools.issues")

issues_server = FastMCP("issues")

@issues_server.tool
async def get_open_issues(
    ctx: Context,
    repo: str,
    branch: str | None = None,
    milestone: str | None = None,
    state: str = "open",
    limit: int = 30,
) -> list[dict]:
    """
    Get issues for a GitHub repository, optionally filtered by branch label or milestone.

    Call this when the user asks about issues, bugs, or tickets on a repo.
    The branch filter matches issues whose labels contain the branch name.

    Parameters: repo in "owner/repo" format, branch and milestone are optional filters.
    """
    client = await get_client(ctx)

    limit = max(1, min(limit, 300))
    max_pages = (limit // 100) + 1
    logger.info("get_open_issues(repo=%s, branch=%s, milestone=%s)", repo, branch, milestone)

    raw_issues = await client.get_issues(
        repo,
        state=state,
        labels=branch,
        milestone=milestone,
        max_pages=max_pages,
        per_page=min(limit, 100),
    )

    # GitHub's issues endpoint also returns PRs — filter them out
    raw_issues = [i for i in raw_issues if "pull_request" not in i]
    raw_issues = raw_issues[:limit]

    issues = [
        {
            "number": i["number"],
            "title": i["title"],
            "state": i["state"],
            "labels": ", ".join(l["name"] for l in i.get("labels", [])),
            "milestone": (i.get("milestone") or {}).get("title"),
            "created_at": i["created_at"],
            "html_url": i["html_url"],
        }
        for i in raw_issues
    ]
    logger.info("Returning %d issues", len(issues))
    return issues
