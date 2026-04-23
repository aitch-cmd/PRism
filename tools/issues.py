from __future__ import annotations
from fastmcp import FastMCP, Context
from core.logger import get_logger
from middleware.auth import get_client

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
    Get general open issues for a GitHub repository.
    
    Call this when the user asks for a list of open bugs, tasks, or issues on a repo.
    IMPORTANT: DO NOT use this tool if the user asks for issues linked to a specific git branch. 
    If they mention a branch, use the `branch_tickets` tool instead.
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

@issues_server.tool
async def branch_tickets(ctx: Context, repo: str, branch_name: str) -> list[dict]:
    """
    Find specific tickets or issues that are linked to a given branch_name.
    
    USE THIS TOOL (instead of get_open_issues) whenever the user asks about issues connected, 
    linked, or related to a branch name. It performs a deep search across issue bodies and labels.
    """
    client = await get_client(ctx)
    logger.info("branch_tickets(repo=%s, branch_name=%s)", repo, branch_name)

    # Search the repo for issues that mention the branch name (which catches PR linked bodies, or labels)
    query = f'repo:{repo} is:issue "{branch_name}"'
    raw_issues = await client.search_issues(query, max_pages=1, per_page=15)

    tickets = [
        {
            "number": i["number"],
            "title": i["title"],
            "state": i["state"],
            "html_url": i["html_url"],
        }
        for i in raw_issues
    ]
    return tickets

