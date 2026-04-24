from __future__ import annotations
import asyncio
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal

from fastmcp import FastMCP, Context
from sqlalchemy import select

from core.db import PRReview
from core.diff_chunker import chunk_diff
from core.logger import get_logger
from middleware.auth import get_client
from middleware.db_session import get_session
from middleware.error_handling import ValidationError

logger = get_logger("prism.tools.prs")

prs_server = FastMCP("prs")

CHUNK_SYSTEM_PROMPT = (
    "You are an expert code reviewer. You are reviewing ONE chunk of a larger "
    "pull request diff. Focus only on the code you see in this chunk.\n\n"
    "Return your findings as terse bullet points under two headings:\n"
    "- **Changes:** what this chunk does (1-3 bullets).\n"
    "- **Risks:** bugs, security issues, perf problems. Omit the heading entirely if none.\n"
    "Do not invent issues. Do not editorialise. No preamble."
)

SYNTHESIS_SYSTEM_PROMPT = (
    "You are an expert code reviewer synthesising per-chunk notes into one "
    "unified review of a pull request. Deduplicate repeated findings. Elevate "
    "the most serious risks.\n\n"
    "Return three sections:\n"
    "1. **Summary:** 2-3 bullets on what the PR does overall.\n"
    "2. **Risk Flags:** consolidated list of bugs/security/perf issues. Say 'None' if clean.\n"
    "3. **Suggested Reviewers:** types of engineers who should look at this, based on the files touched."
)

INCREMENTAL_SYSTEM_PROMPT = (
    "You are re-reviewing a pull request that you have seen before. You are "
    "given (a) your prior review and (b) ONLY the code that has changed since "
    "that review — not the whole PR.\n\n"
    "Flag only NEW issues introduced since the last review. If a prior concern "
    "now looks resolved, call it out briefly. Do not re-list findings that "
    "still apply unchanged — the reader already has them.\n\n"
    "Return two sections:\n"
    "1. **What changed since last review:** 1-3 bullets.\n"
    "2. **New risks / resolved items:** bullets, or 'No new issues.'"
)


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

    diff_text = await client.get_pr_diff(repo, pr_number)

    lines = diff_text.splitlines()
    if len(lines) > max_lines:
        logger.info("Truncating diff from %d lines to %d", len(lines), max_lines)
        lines = lines[:max_lines]
        lines.append(f"\n... [Diff truncated at {max_lines} lines] ...")

    return "\n".join(lines)


async def _review_chunks(ctx: Context, diff_text: str) -> str:
    """Map-reduce over the diff: review each chunk, then synthesise."""
    chunks = chunk_diff(diff_text)
    if not chunks:
        return "PR has no reviewable code changes."

    if len(chunks) == 1:
        await ctx.info(f"Reviewing {len(chunks[0].files)} file(s) in a single pass...")
        result = await ctx.sample(
            messages=chunks[0].text,
            system_prompt=SYNTHESIS_SYSTEM_PROMPT,
            max_tokens=1500,
        )
        return result.text

    await ctx.info(f"Diff split into {len(chunks)} chunks. Reviewing in parallel...")
    chunk_reviews = await asyncio.gather(
        *(
            ctx.sample(
                messages=(
                    f"Files in this chunk: {', '.join(chunk.paths)}\n\n"
                    f"{chunk.text}"
                ),
                system_prompt=CHUNK_SYSTEM_PROMPT,
                max_tokens=800,
            )
            for chunk in chunks
        )
    )

    await ctx.info("Synthesising chunk reviews into final report...")
    bundled = "\n\n---\n\n".join(
        f"### Chunk {i + 1} — files: {', '.join(chunk.paths)}\n{res.text}"
        for i, (chunk, res) in enumerate(zip(chunks, chunk_reviews))
    )
    synthesis = await ctx.sample(
        messages=bundled,
        system_prompt=SYNTHESIS_SYSTEM_PROMPT,
        max_tokens=1500,
    )
    return synthesis.text


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
        review_body = await _review_chunks(ctx, diff_text)

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


# ---------------------------------------------------------------------------
# Reviewer assignment
# ---------------------------------------------------------------------------

OOO_THRESHOLD_DAYS = 7  # no public activity within this window → flag as OOO


async def _blame_tally(client, repo: str, pr_number: int, pr_author: str) -> Counter:
    files = await client.get_pr_files(repo, pr_number)
    if not files:
        return Counter()
    files = sorted(files, key=lambda f: f.get("changes", 0), reverse=True)[:5]

    tally: Counter = Counter()
    for f in files:
        filename = f.get("filename")
        if not filename:
            continue
        history = await client.get_file_commit_history(repo, filename)
        for commit in history:
            author = commit.get("author") or {}
            login = author.get("login")
            if login and login != pr_author and "[bot]" not in login and login != "web-flow":
                tally[login] += 1
    return tally


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _hours_between(a: datetime, b: datetime) -> float:
    return abs((b - a).total_seconds()) / 3600.0


async def _reviewer_availability(client, login: str) -> dict[str, Any]:
    """
    Returns:
      - open_prs: how many PRs currently await this user's review
      - avg_turnaround_hours: from their last ~10 reviews
      - days_since_activity: last public event (commit/review/comment/push)
      - ooo: True if silent for > OOO_THRESHOLD_DAYS
    """
    open_prs_q = f"type:pr state:open review-requested:{login}"
    try:
        open_prs = await client.search_issues_count(open_prs_q)
    except Exception:
        open_prs = 0

    reviewed_q = f"type:pr reviewed-by:{login}"
    try:
        recent_reviewed = await client.search_issues(reviewed_q, max_pages=1, per_page=10)
    except Exception:
        recent_reviewed = []

    turnaround_samples: list[float] = []
    for pr in recent_reviewed[:5]:
        repo_full = pr["repository_url"].split("/repos/")[-1]
        number = pr["number"]
        created = pr.get("created_at")
        try:
            reviews = await client.get_pr_reviews(repo_full, number)
        except Exception:
            continue
        first_by_user = next(
            (r for r in reviews if (r.get("user") or {}).get("login") == login),
            None,
        )
        if not first_by_user or not created:
            continue
        submitted = first_by_user.get("submitted_at")
        if not submitted:
            continue
        t_created = _parse_iso(created)
        t_submitted = _parse_iso(submitted)
        if t_created and t_submitted:
            turnaround_samples.append(_hours_between(t_created, t_submitted))

    avg_turnaround = (
        sum(turnaround_samples) / len(turnaround_samples)
        if turnaround_samples
        else None
    )

    days_since_activity: float | None = None
    try:
        events = await client.get_user_events(login)
        if events:
            latest = _parse_iso(events[0].get("created_at", ""))
            if latest:
                days_since_activity = (
                    _hours_between(latest, datetime.now(timezone.utc)) / 24.0
                )
    except Exception:
        pass

    ooo = (
        days_since_activity is not None
        and days_since_activity > OOO_THRESHOLD_DAYS
    )

    return {
        "open_prs": open_prs,
        "avg_turnaround_hours": avg_turnaround,
        "days_since_activity": days_since_activity,
        "ooo": ooo,
    }


def _score(blame_weight: int, stats: dict[str, Any]) -> float:
    """
    Higher = better candidate. Purely additive so it's readable.

    - blame weight is the dominant factor (domain knowledge)
    - subtract for open-PR load (saturates at ~10)
    - reward fast turnaround (bounded at 48h), penalise slow
    - hard-penalty if OOO
    """
    score = float(blame_weight) * 10.0
    score -= min(stats["open_prs"], 10) * 2.0
    t = stats["avg_turnaround_hours"]
    if t is not None:
        score += max(0.0, 48.0 - min(t, 96.0)) / 4.0
    if stats["ooo"]:
        score -= 50.0
    return score


@prs_server.tool
async def assign_reviewer(ctx: Context, repo: str, pr_number: int) -> str:
    """
    Pick the best reviewer(s) for a PR.

    Combines four signals:
      1. File-history blame (who has historically owned the touched files).
      2. Current open-PR load (don't pile onto someone already drowning).
      3. Recent review turnaround time (reward responsive reviewers).
      4. OOO detection (no public GitHub activity in the last week).

    Returns a ranked list with reasoning and auto-requests the top candidate.
    """
    client = await get_client(ctx)
    logger.info("assign_reviewer(repo=%s, pr_number=%d)", repo, pr_number)

    pr_detail = await client.get_pr_detail(repo, pr_number)
    pr_author = pr_detail.get("user", {}).get("login")
    if not pr_author:
        raise ValidationError(f"Could not determine author of {repo}#{pr_number}")

    await ctx.info("Computing blame-based candidate pool...")
    blame = await _blame_tally(client, repo, pr_number, pr_author)
    if not blame:
        raise ValidationError(
            f"No past contributors found for {repo}#{pr_number} (other than the author)."
        )

    candidates = [login for login, _ in blame.most_common(5)]
    await ctx.info(f"Scoring availability for {len(candidates)} candidates...")

    stats_list = await asyncio.gather(
        *(_reviewer_availability(client, c) for c in candidates)
    )

    ranked = sorted(
        [
            {
                "login": login,
                "blame_weight": blame[login],
                "score": _score(blame[login], stats),
                **stats,
            }
            for login, stats in zip(candidates, stats_list)
        ],
        key=lambda r: r["score"],
        reverse=True,
    )

    eligible = [r for r in ranked if not r["ooo"]]
    top = eligible[:2] if eligible else []

    if not top:
        # Partial-success return: all candidates OOO is a domain outcome, not a
        # tool failure — the ranking is still useful to the caller.
        return (
            "⚠️ All candidates appear out-of-office. "
            f"Ranked candidates:\n{_format_ranking(ranked)}"
        )

    top_logins = [r["login"] for r in top]
    await client.request_pr_review(repo, pr_number, top_logins)

    return (
        f"✅ Requested review from: {', '.join(top_logins)}\n\n"
        f"### Ranking\n{_format_ranking(ranked)}"
    )


def _format_ranking(ranked: list[dict[str, Any]]) -> str:
    lines = []
    for i, r in enumerate(ranked, 1):
        turnaround = (
            f"{r['avg_turnaround_hours']:.1f}h"
            if r["avg_turnaround_hours"] is not None
            else "n/a"
        )
        activity = (
            f"{r['days_since_activity']:.1f}d ago"
            if r["days_since_activity"] is not None
            else "unknown"
        )
        flags = " 🚫 OOO" if r["ooo"] else ""
        lines.append(
            f"{i}. **{r['login']}** — score {r['score']:.1f}"
            f" · blame {r['blame_weight']}"
            f" · {r['open_prs']} open reviews"
            f" · turnaround {turnaround}"
            f" · last active {activity}{flags}"
        )
    return "\n".join(lines)
