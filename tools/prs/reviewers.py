"""Reviewer assignment (post-open) and pre-flight diff analysis (pre-open)."""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from fastmcp import Context

from core.logger import get_logger
from middleware.auth import get_client
from middleware.error_handling import ValidationError

from ._server import prs_server
from ._shared import (
    hours_between,
    is_config_file,
    is_test_file,
    parse_iso,
    size_bucket,
)

logger = get_logger("prism.tools.prs.reviewers")

OOO_THRESHOLD_DAYS = 7  # no public activity within this window → flag as OOO


# ---------------------------------------------------------------------------
# Blame + availability scoring (shared by assign_reviewer and suggest_reviewers_for_diff)
# ---------------------------------------------------------------------------

async def _blame_tally_for_paths(
    client, repo: str, paths: list[str], exclude_login: str | None
) -> Counter:
    """Walk recent commit history on each path, tally authors (excluding bots / PR author)."""
    tally: Counter = Counter()
    for filename in paths:
        if not filename:
            continue
        history = await client.get_file_commit_history(repo, filename)
        for commit in history:
            author = commit.get("author") or {}
            login = author.get("login")
            if (
                login
                and login != exclude_login
                and "[bot]" not in login
                and login != "web-flow"
            ):
                tally[login] += 1
    return tally


async def _blame_tally(client, repo: str, pr_number: int, pr_author: str) -> Counter:
    files = await client.get_pr_files(repo, pr_number)
    if not files:
        return Counter()
    files = sorted(files, key=lambda f: f.get("changes", 0), reverse=True)[:5]
    paths = [f.get("filename") for f in files if f.get("filename")]
    return await _blame_tally_for_paths(client, repo, paths, pr_author)


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
        t_created = parse_iso(created)
        t_submitted = parse_iso(submitted)
        if t_created and t_submitted:
            turnaround_samples.append(hours_between(t_created, t_submitted))

    avg_turnaround = (
        sum(turnaround_samples) / len(turnaround_samples)
        if turnaround_samples
        else None
    )

    days_since_activity: float | None = None
    try:
        events = await client.get_user_events(login)
        if events:
            latest = parse_iso(events[0].get("created_at", ""))
            if latest:
                days_since_activity = (
                    hours_between(latest, datetime.now(timezone.utc)) / 24.0
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


# ---------------------------------------------------------------------------
# Post-open reviewer assignment
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Pre-flight diff analysis (no PR required)
# ---------------------------------------------------------------------------

# Heuristic review-time model — calibrated against the rule-of-thumb that a
# seasoned reviewer sustains ~200-400 lines/hour on novel code and skims
# tests/config faster. All weights in lines-equivalent; divided by 300 at the
# end to get hours.
_LINES_PER_HOUR = 300.0
_PER_FILE_OVERHEAD = 15.0  # context-switch cost per extra file touched
_TEST_WEIGHT = 0.4
_CONFIG_WEIGHT = 0.3
_CODE_WEIGHT = 1.0

# Splitting thresholds — fire a split suggestion if EITHER holds, unless the
# change is cohesive (≤1 top-level module touched).
_SPLIT_LINES = 500
_SPLIT_FILES = 15
_SPLIT_MODULES = 3


def _top_module(path: str) -> str:
    # First non-dot segment — e.g. "tools/prs.py" → "tools", "src/a/b.ts" → "src".
    parts = [seg for seg in path.split("/") if seg and not seg.startswith(".")]
    return parts[0] if parts else "(root)"


def _estimate_review_hours(files: list[dict[str, Any]]) -> float:
    weighted_lines = 0.0
    for f in files:
        path = f.get("filename") or ""
        changes = int(f.get("changes") or 0)
        if is_test_file(path):
            weight = _TEST_WEIGHT
        elif is_config_file(path):
            weight = _CONFIG_WEIGHT
        else:
            weight = _CODE_WEIGHT
        weighted_lines += changes * weight
    overhead = _PER_FILE_OVERHEAD * max(len(files) - 1, 0)
    return (weighted_lines + overhead) / _LINES_PER_HOUR


def _split_suggestion(files: list[dict[str, Any]]) -> dict[str, Any]:
    """Group by top-level module; recommend a split when the PR is both big and spread out."""
    groups: dict[str, dict[str, Any]] = {}
    for f in files:
        path = f.get("filename") or ""
        mod = _top_module(path)
        g = groups.setdefault(mod, {"module": mod, "files": [], "changes": 0})
        g["files"].append(path)
        g["changes"] += int(f.get("changes") or 0)

    ordered = sorted(groups.values(), key=lambda g: g["changes"], reverse=True)
    total_changes = sum(g["changes"] for g in ordered)
    total_files = sum(len(g["files"]) for g in ordered)
    module_count = len(ordered)

    should_split = (
        (total_changes > _SPLIT_LINES or total_files > _SPLIT_FILES)
        and module_count >= _SPLIT_MODULES
    )

    reasons: list[str] = []
    if total_changes > _SPLIT_LINES:
        reasons.append(f"{total_changes} changed lines exceeds {_SPLIT_LINES}-line split threshold")
    if total_files > _SPLIT_FILES:
        reasons.append(f"{total_files} files exceeds {_SPLIT_FILES}-file split threshold")
    if module_count >= _SPLIT_MODULES:
        reasons.append(f"changes span {module_count} top-level modules")
    if not should_split and module_count < _SPLIT_MODULES:
        reasons.append("change is cohesive to one or two modules — keep as one PR")

    return {
        "should_split": should_split,
        "reasons": reasons,
        "groups": [
            {
                "module": g["module"],
                "file_count": len(g["files"]),
                "changes": g["changes"],
                "sample_files": g["files"][:5],
            }
            for g in ordered
        ],
    }


@prs_server.tool
async def suggest_reviewers_for_diff(
    ctx: Context,
    repo: str,
    branch: str,
    base: str | None = None,
    top_n: int = 3,
) -> dict:
    """
    Pre-flight analysis of a branch BEFORE a PR is opened.

    Analyses the diff against `base` (default: repo default branch) and returns:
      - Ranked reviewer suggestions with blame weight, current load, and turnaround
        — but does NOT request reviews (that's what `assign_reviewer` does after
        the PR is opened).
      - Estimated review time in minutes, weighted by code vs test vs config.
      - Size category and a split recommendation with per-module groupings if
        the change is both big AND spread across modules.

    Use this when the user says "should I split this PR?", "who should review
    my branch?", or "how big is my change?" before opening the PR.
    """
    client = await get_client(ctx)
    logger.info(
        "suggest_reviewers_for_diff(repo=%s, branch=%s, base=%s)",
        repo, branch, base,
    )

    if base is None:
        repo_info = await client.get_repo(repo)
        base = repo_info.get("default_branch") or "main"

    if base == branch:
        raise ValidationError("Base and head branches are the same — nothing to analyse.")

    try:
        comparison = await client.compare_commits(repo, base, branch)
    except Exception as exc:
        raise ValidationError(
            f"Could not compare {base}...{branch} on {repo}: {exc}"
        ) from exc

    files = comparison.get("files") or []
    commits = comparison.get("commits") or []
    if not files:
        return {
            "repo": repo,
            "branch": branch,
            "base": base,
            "note": f"No file changes on {branch} ahead of {base}.",
            "stats": {"files": 0, "additions": 0, "deletions": 0, "changes": 0, "commits": 0},
            "reviewers": [],
            "split_suggestion": {"should_split": False, "reasons": [], "groups": []},
        }

    # Infer branch author from the commits we just fetched — we have no PR yet.
    branch_author: str | None = None
    for c in commits:
        author = (c.get("author") or {}).get("login")
        if author and "[bot]" not in author:
            branch_author = author
            break

    total_additions = sum(int(f.get("additions") or 0) for f in files)
    total_deletions = sum(int(f.get("deletions") or 0) for f in files)
    total_changes = sum(int(f.get("changes") or 0) for f in files)

    stats = {
        "files": len(files),
        "additions": total_additions,
        "deletions": total_deletions,
        "changes": total_changes,
        "commits": len(commits),
    }

    review_hours = _estimate_review_hours(files)
    review_minutes = max(5, int(round(review_hours * 60)))

    size, _ = size_bucket(total_changes, len(files))
    split = _split_suggestion(files)

    top_paths = [
        f.get("filename")
        for f in sorted(files, key=lambda f: int(f.get("changes") or 0), reverse=True)[:5]
        if f.get("filename")
    ]

    await ctx.info(f"Tallying blame across {len(top_paths)} hottest files...")
    blame = await _blame_tally_for_paths(client, repo, top_paths, branch_author)

    reviewers: list[dict[str, Any]] = []
    if blame:
        candidates = [login for login, _ in blame.most_common(max(top_n * 2, 4))]
        await ctx.info(f"Scoring availability for {len(candidates)} candidates...")
        stats_list = await asyncio.gather(
            *(_reviewer_availability(client, c) for c in candidates)
        )
        ranked = sorted(
            [
                {
                    "login": login,
                    "blame_weight": blame[login],
                    "score": _score(blame[login], s),
                    **s,
                }
                for login, s in zip(candidates, stats_list)
            ],
            key=lambda r: r["score"],
            reverse=True,
        )
        reviewers = ranked[:top_n]

    return {
        "repo": repo,
        "branch": branch,
        "base": base,
        "branch_author": branch_author,
        "stats": stats,
        "size_category": size,
        "estimated_review_minutes": review_minutes,
        "reviewers": reviewers,
        "split_suggestion": split,
        "top_files": [
            {
                "filename": f.get("filename"),
                "changes": int(f.get("changes") or 0),
                "additions": int(f.get("additions") or 0),
                "deletions": int(f.get("deletions") or 0),
                "status": f.get("status"),
            }
            for f in sorted(files, key=lambda f: int(f.get("changes") or 0), reverse=True)[:10]
        ],
    }
