from __future__ import annotations
from typing import Literal
from fastmcp import FastMCP, Context
from logger import get_logger
from tools.auth import get_client

logger = get_logger("prism.tools.prs")

prs_server = FastMCP("prs")

@prs_server.tool
async def get_my_prs(
    ctx: Context,
    state: Literal["open", "closed", "merged"] = "open",
    limit: int = 30,
) -> list[dict]:
    """
    Get pull requests where the authenticated user is the author or assignee.
    Searches across ALL repos — not limited to a single repo.

    Call this when the user asks about their PRs, pull requests, or code reviews.
    For example: "What PRs do I have open?", "Show my merged PRs".
    """
    client = await get_client(ctx)
    user = await ctx.get_state("github_user")
    if not user:
        me = await client.get_authenticated_user()
        user = me["login"]

    limit = max(1, min(limit, 100))
    logger.info("get_my_prs(state=%s, limit=%d, user=%s)", state, limit, user)

    # GitHub Search: type:pr + author or assignee
    # "merged" is not a GitHub PR state — it's closed + merged
    if state == "merged":
        query = f"type:pr author:{user} is:merged"
    else:
        query = f"type:pr author:{user} is:{state}"

    raw_prs = await client.search_issues(query, max_pages=1, per_page=limit)
    raw_prs = raw_prs[:limit]

    prs = []
    for pr in raw_prs:
        repo = pr["repository_url"].split("/repos/")[-1]
        number = pr["number"]

        # Fetch extra info for CI and Reviews
        pr_detail = await client.get_pr_detail(repo, number)
        sha = pr_detail["head"]["sha"]
        branch = pr_detail["head"]["ref"]

        ci_status = await client.get_commit_status(repo, sha)

        reviews = await client.get_pr_reviews(repo, number)
        review_states = [r["state"] for r in reviews]
        if "CHANGES_REQUESTED" in review_states:
            review_status = "CHANGES_REQUESTED"
        elif "APPROVED" in review_states:
            review_status = "APPROVED"
        else:
            review_status = "PENDING"

        prs.append({
            "number": number,
            "title": pr["title"],
            "repo": repo,
            "branch": branch,
            "state": "merged" if pr.get("pull_request", {}).get("merged_at") else pr["state"],
            "ci_status": ci_status,
            "review_status": review_status,
            "created_at": pr["created_at"],
            "html_url": pr["html_url"],
        })

    logger.info("Returning %d PRs", len(prs))
    return prs

@prs_server.tool
async def get_pr_diff(
    ctx: Context,
    repo: str,
    pr_number: int,
    max_lines: int = 500,
) -> str:
    """
    Get the raw unified diff for a pull request.
    Call this when the user asks to see what changes are in a PR,
    or wants you to summarize or review a PR.
    Returns the diff as a string (truncated if it exceeds max_lines).
    """
    client = await get_client(ctx)
    logger.info("get_pr_diff(repo=%s, pr_number=%d, max_lines=%d)", repo, pr_number, max_lines)

    try:
        diff_text = await client.get_pr_diff(repo, pr_number)
    except Exception as exc:
        logger.warning("Failed to fetch diff: %s", exc)
        return f"❌ Error fetching PR diff: {exc}"

    lines = diff_text.splitlines()
    if len(lines) > max_lines:
        logger.info("Truncating diff from %d lines to %d", len(lines), max_lines)
        lines = lines[:max_lines]
        lines.append(f"\n... [Diff truncated at {max_lines} lines] ...")

    return "\n".join(lines)

