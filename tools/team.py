"""
Team-level shipping metrics aggregated from PR history.

PRism's other tools answer "do this PR". This one answers "how is the team
shipping" — review latency, PR size shape, revert rate, CI flake rate, with
a per-author breakdown. It reads only what's already in GitHub (search +
PR detail + review list + check runs), so no extra wiring required.
"""
from __future__ import annotations

import asyncio
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastmcp import Context, FastMCP

from core.logger import get_logger
from middleware.auth import get_client
from middleware.error_handling import ValidationError
from tools.prs._shared import flaky_check_names, hours_between, parse_date, parse_iso

logger = get_logger("prism.tools.team")

team_server = FastMCP("team")


_REVERT_TITLE_RE = re.compile(r'^revert[\s:"\']', re.IGNORECASE)
_REVERTS_BODY_RE = re.compile(r'this reverts commit', re.IGNORECASE)

_SIZE_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("tiny",   0,    50),
    ("small",  51,   200),
    ("medium", 201,  500),
    ("large",  501,  1500),
    ("huge",   1501, 10**9),
)


def _bucket(size: int) -> str:
    for name, lo, hi in _SIZE_BUCKETS:
        if lo <= size <= hi:
            return name
    return "huge"


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo, hi = int(k), int(k) + 1
    if hi >= len(s):
        return s[-1]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _is_revert(pr: dict[str, Any]) -> bool:
    title = pr.get("title") or ""
    body = pr.get("body") or ""
    return bool(_REVERT_TITLE_RE.match(title) or _REVERTS_BODY_RE.search(body))


def _summarise(
    target: str,
    merged: int,
    reverts: int,
    latencies: list[float],
    flake_rate: float | None,
) -> str:
    if merged == 0:
        return f"No merged PRs in window for {target}."
    parts: list[str] = [f"**{target}** — {merged} merged PR(s)"]
    if latencies:
        parts.append(f"median review latency {statistics.median(latencies):.1f}h")
    revert_pct = (reverts / merged) * 100
    parts.append(f"revert rate {revert_pct:.1f}%")
    if flake_rate is not None:
        parts.append(f"CI flake rate {flake_rate*100:.1f}%")
    return "; ".join(parts) + "."


@team_server.tool
async def team_health(
    ctx: Context,
    repo: str | None = None,
    org: str | None = None,
    since: str | None = None,
    until: str | None = None,
    top_n_authors: int = 10,
    pr_sample_limit: int = 150,
    flake_check_limit: int = 50,
) -> dict:
    """
    Aggregate team shipping metrics over a time window.

    Provide EITHER `repo` ("owner/name") OR `org` to scope the query.
    Default window is the last 30 days; pass `since`/`until` as ISO-8601 dates
    or timestamps to override.

    Computes:
      - Throughput: merged PRs in window + per-week rate.
      - Size: median, p90, and tiny/small/medium/large/huge bucket distribution.
      - Review latency: hours from PR open → first non-author review submission
        (median + p90, both sample-size annotated).
      - Revert rate: PRs whose title starts "Revert" or whose body contains
        "This reverts commit", as a fraction of merged PRs.
      - CI flake rate: fraction of sampled PRs where the same check name on
        the same head SHA had at least one `failure` and a later `success`
        — i.e. green-on-retry. Sampled separately to keep API cost bounded.
      - Per-author rollup (top N): PR count, median size, median review
        latency, reverts authored.

    Use this when the user asks "how is the team shipping?", "what's our
    review latency this month?", "is CI flaky?", or "who's been carrying
    this repo?".
    """
    if not repo and not org:
        raise ValidationError("Provide `repo` or `org`.")
    if repo and org:
        raise ValidationError("Provide `repo` OR `org`, not both.")

    until_dt = parse_date(until) if until else datetime.now(timezone.utc)
    since_dt = parse_date(since) if since else (until_dt - timedelta(days=30))
    if since_dt is None or until_dt is None:
        raise ValidationError("`since` and `until` must be ISO-8601 dates.")
    if since_dt >= until_dt:
        raise ValidationError("`since` must be before `until`.")

    client = await get_client(ctx)
    scope = f"repo:{repo}" if repo else f"org:{org}"
    query = (
        f"{scope} type:pr is:merged "
        f"merged:{since_dt.date().isoformat()}..{until_dt.date().isoformat()}"
    )
    pr_sample_limit = max(10, min(pr_sample_limit, 500))
    max_pages = max(1, (pr_sample_limit + 99) // 100)

    await ctx.info(f"Fetching merged PRs: {query}")
    raw = await client.search_issues(query, max_pages=max_pages, per_page=100)
    raw = raw[:pr_sample_limit]

    if not raw:
        return {
            "scope": "repo" if repo else "org",
            "target": repo or org,
            "since": since_dt.isoformat(),
            "until": until_dt.isoformat(),
            "merged_prs": 0,
            "summary": f"No merged PRs in window for {repo or org}.",
        }

    sem = asyncio.Semaphore(8)

    async def _hydrate(pr: dict[str, Any]) -> dict[str, Any] | None:
        async with sem:
            repo_full = pr["repository_url"].split("/repos/")[-1]
            number = pr["number"]
            try:
                detail, reviews = await asyncio.gather(
                    client.get_pr_detail(repo_full, number),
                    client.get_pr_reviews(repo_full, number),
                )
            except Exception as exc:
                logger.warning(
                    "hydrate failed %s#%s: %s", repo_full, number, exc
                )
                return None
            return {
                "search": pr,
                "repo": repo_full,
                "number": number,
                "detail": detail,
                "reviews": reviews if isinstance(reviews, list) else [],
            }

    await ctx.info(f"Hydrating {len(raw)} PR(s)...")
    hydrated_raw = await asyncio.gather(*(_hydrate(p) for p in raw))
    hydrated = [h for h in hydrated_raw if h is not None]

    sizes: list[int] = []
    review_latencies: list[float] = []
    revert_count = 0
    bucket_counts: Counter = Counter()
    by_author: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"sizes": [], "latencies": [], "reverts": 0}
    )

    for h in hydrated:
        pr = h["search"]
        detail = h["detail"]
        reviews = h["reviews"]
        author = (pr.get("user") or {}).get("login") or "unknown"

        adds = int(detail.get("additions") or 0)
        dels = int(detail.get("deletions") or 0)
        size = adds + dels
        sizes.append(size)
        bucket_counts[_bucket(size)] += 1
        by_author[author]["sizes"].append(size)

        created = parse_iso(pr.get("created_at") or "")
        first_review_t: datetime | None = None
        for r in reviews:
            login = (r.get("user") or {}).get("login")
            if not login or login == author:
                continue
            t = parse_iso(r.get("submitted_at") or "")
            if not t:
                continue
            if first_review_t is None or t < first_review_t:
                first_review_t = t
        if created and first_review_t:
            hrs = hours_between(created, first_review_t)
            review_latencies.append(hrs)
            by_author[author]["latencies"].append(hrs)

        if _is_revert(pr):
            revert_count += 1
            by_author[author]["reverts"] += 1

    flake_check_limit = max(0, min(flake_check_limit, len(hydrated)))
    flake_sample = hydrated[:flake_check_limit]
    flake_pr_count = 0
    flake_pr_total = 0

    async def _check_flake(h: dict[str, Any]) -> bool | None:
        async with sem:
            head_sha = (h["detail"].get("head") or {}).get("sha")
            if not head_sha:
                return None
            try:
                runs = await client.get_check_runs(h["repo"], head_sha)
            except Exception as exc:
                logger.debug(
                    "check_runs failed %s@%s: %s", h["repo"], head_sha[:7], exc
                )
                return None
            if not runs:
                return None
            return bool(flaky_check_names(runs))

    if flake_sample:
        await ctx.info(f"Checking CI flake on {len(flake_sample)} PR(s)...")
        for outcome in await asyncio.gather(
            *(_check_flake(h) for h in flake_sample)
        ):
            if outcome is None:
                continue
            flake_pr_total += 1
            if outcome:
                flake_pr_count += 1

    flake_rate = (flake_pr_count / flake_pr_total) if flake_pr_total else None

    author_rows: list[dict[str, Any]] = []
    for author, data in by_author.items():
        if not data["sizes"]:
            continue
        author_rows.append({
            "author": author,
            "merged_prs": len(data["sizes"]),
            "median_size_lines": int(statistics.median(data["sizes"])),
            "median_review_latency_hours": (
                round(statistics.median(data["latencies"]), 1)
                if data["latencies"] else None
            ),
            "reverts_authored": data["reverts"],
        })
    author_rows.sort(key=lambda a: a["merged_prs"], reverse=True)
    author_rows = author_rows[: max(1, top_n_authors)]

    days = max((until_dt - since_dt).total_seconds() / 86400.0, 1.0)
    throughput_per_week = round(len(hydrated) * 7.0 / days, 1)

    return {
        "scope": "repo" if repo else "org",
        "target": repo or org,
        "since": since_dt.isoformat(),
        "until": until_dt.isoformat(),
        "merged_prs": len(hydrated),
        "throughput_per_week": throughput_per_week,
        "size": {
            "median_lines": int(statistics.median(sizes)) if sizes else 0,
            "p90_lines": int(_percentile([float(s) for s in sizes], 0.9) or 0),
            "distribution": {
                name: bucket_counts.get(name, 0)
                for name, _, _ in _SIZE_BUCKETS
            },
        },
        "review_latency": {
            "median_hours": (
                round(statistics.median(review_latencies), 1)
                if review_latencies else None
            ),
            "p90_hours": (
                round(_percentile(review_latencies, 0.9) or 0.0, 1)
                if review_latencies else None
            ),
            "sample_size": len(review_latencies),
        },
        "revert_rate": round(revert_count / len(hydrated), 3),
        "revert_count": revert_count,
        "ci_flake_rate": (
            round(flake_rate, 3) if flake_rate is not None else None
        ),
        "ci_flake_sample": flake_pr_total,
        "by_author": author_rows,
        "summary": _summarise(
            repo or org, len(hydrated), revert_count, review_latencies, flake_rate
        ),
    }
