"""Core PR tools: listing, diffing, reviewing, CI status, commenting."""
from __future__ import annotations

from typing import Any, Literal

from fastmcp import Context
from sqlalchemy import select

from core.db import PRReview
from core.logger import get_logger
from middleware.auth import get_client
from middleware.db_session import get_session
from middleware.error_handling import ValidationError

from ._server import prs_server
from ._shared import INCREMENTAL_SYSTEM_PROMPT, review_chunks

logger = get_logger("prism.tools.prs.core")


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

        pr_detail = await client.get_pr_detail(repo, number)
        sha = pr_detail["head"]["sha"]
        branch = pr_detail["head"]["ref"]

        ci_state = await client.get_commit_status(repo, sha)

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
            "ci_status": ci_state,
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

    diff_text = await client.get_pr_diff(repo, pr_number)

    lines = diff_text.splitlines()
    if len(lines) > max_lines:
        logger.info("Truncating diff from %d lines to %d", len(lines), max_lines)
        lines = lines[:max_lines]
        lines.append(f"\n... [Diff truncated at {max_lines} lines] ...")

    return "\n".join(lines)


@prs_server.tool
async def review_pr(ctx: Context, repo: str, pr_number: int) -> str:
    """
    Review a pull request by analyzing its raw diff.

    Stateful: if this PR has been reviewed before, only the code pushed since
    the last review is re-analyzed, and the response highlights NEW issues
    instead of re-listing old ones. Large diffs are chunked and map-reduced
    so PRs over ~2k lines review reliably.

    Returns a summary, risk flags, and suggested reviewer types.
    """
    client = await get_client(ctx)
    session = await get_session(ctx)

    pr_detail = await client.get_pr_detail(repo, pr_number)

    head_sha = pr_detail.get("head", {}).get("sha")
    if not head_sha:
        raise ValidationError(f"Could not resolve head SHA for {repo}#{pr_number}")

    prior: dict[str, Any] | None = None
    if session is not None:
        stmt = (
            select(PRReview)
            .where(PRReview.repo == repo, PRReview.pr_number == pr_number)
            .order_by(PRReview.created_at.desc())
            .limit(1)
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            prior = {
                "head_sha": row.head_sha,
                "body": row.body,
                "created_at": row.created_at,
            }

    if prior and prior["head_sha"] == head_sha:
        return prior["body"] + "\n\n_(unchanged since last review)_"

    if prior:
        await ctx.info(
            f"Incremental review: diffing {prior['head_sha'][:7]}..{head_sha[:7]}"
        )
        diff_text = await client.compare_commits_diff(
            repo, prior["head_sha"], head_sha
        )
    else:
        await ctx.info(f"First-time review of {repo}#{pr_number}")
        diff_text = await client.get_pr_diff(repo, pr_number)

    if not diff_text.strip():
        return "No code changes detected since the last review."

    if prior:
        await ctx.info("Running incremental review against prior findings...")
        result = await ctx.sample(
            messages=(
                "## Prior review (for reference only)\n"
                f"{prior['body']}\n\n"
                "## New code since that review\n"
                f"{diff_text}"
            ),
            system_prompt=INCREMENTAL_SYSTEM_PROMPT,
            max_tokens=1200,
        )
        review_body = result.text
    else:
        review_body = await review_chunks(ctx, diff_text)

    if session is not None:
        session.add(
            PRReview(
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                body=review_body,
            )
        )
        # DatabaseSessionMiddleware commits on successful return, rolls back on raise.
        logger.info("Queued review for %s#%d @ %s", repo, pr_number, head_sha[:7])

    return review_body


@prs_server.tool
async def ci_status(ctx: Context, repo: str, pr_number: int) -> str:
    """
    Check the GitHub Actions CI run status for a Pull Request.
    Returns a minimal combined state like 'success', 'pending', or 'failure'.
    """
    client = await get_client(ctx)
    logger.info("ci_status(repo=%s, pr_number=%d)", repo, pr_number)

    pr_detail = await client.get_pr_detail(repo, pr_number)
    sha = pr_detail.get("head", {}).get("sha")
    if not sha:
        raise ValidationError(f"Could not resolve head SHA for {repo}#{pr_number}")

    return await client.get_commit_status(repo, sha)


@prs_server.tool
async def comment_on_pr(ctx: Context, repo: str, pr_number: int, body: str) -> str:
    """
    Post a review comment on a pull request.
    Use this to provide feedback, ask questions, or summarize findings on a PR.
    """
    client = await get_client(ctx)
    logger.info("comment_on_pr(repo=%s, pr_number=%d)", repo, pr_number)

    result = await client.create_pr_comment(repo, pr_number, body)
    return f"✅ Successfully posted comment on PR #{pr_number}. URL: {result.get('html_url')}"
