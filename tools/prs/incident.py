"""
"Which PR caused this?" — rank candidate PRs that landed in a time window
and overlap with an incident description.

Read-only, GitHub-only, no LLM. Combines:
  - merged-PR search by date window (or derived from a SHA's commit time),
  - file-path matching against tokens from the description,
  - title/body keyword overlap,
  - recency-decayed weight (newer PRs in the window rank higher),
  - a small domain-keyword boost when the description hints at a domain
    (db/auth/payments/...) and the PR touches paths in that domain.

The scoring shape mirrors `find_related_prs` so callers see a familiar
breakdown of WHY each PR ranked where it did.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastmcp import Context

from core.logger import get_logger
from middleware.auth import get_client
from middleware.error_handling import ValidationError

from ._server import prs_server
from ._shared import parse_date, parse_iso

logger = get_logger("prism.tools.prs.incident")


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
# Split path components AND underscore-joined names so `stripe_webhook.py`
# tokenises as {stripe, webhook} and matches a description that says
# "stripe webhook" with two words.
_PATH_PART_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")

_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "have", "has", "will", "that", "this",
    "into", "when", "where", "what", "why", "how", "are", "was", "were", "been",
    "being", "they", "them", "their", "there", "here", "also", "only", "just",
    "about", "some", "any", "each", "every", "other", "such", "very", "too",
    "use", "using", "used", "make", "made", "does", "did", "doing",
    "not", "but", "yet", "its", "our", "your", "his", "her",
    "issue", "bug", "broken", "broke", "fail", "failed", "failing", "error",
    "errors", "exception", "problem", "problems", "started", "since", "after",
    "before", "today", "yesterday", "morning", "evening",
})


def _tokens(text: str) -> set[str]:
    return {
        t.lower()
        for t in _WORD_RE.findall(text or "")
        if t.lower() not in _STOPWORDS and len(t) >= 3
    }


def _path_tokens(path: str) -> set[str]:
    return {
        p.lower()
        for p in _PATH_PART_RE.findall(path or "")
        if len(p) >= 3
    }


# ---------------------------------------------------------------------------
# Domain keyword boost
# ---------------------------------------------------------------------------

# Keyword in description → path substrings that earn a boost when matched.
_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "database":     ("db/", "models/", "migration", "schema", "query"),
    "db":           ("db/", "models/", "migration", "schema"),
    "query":        ("db/", "models/", "query", "sql"),
    "sql":          ("db/", "sql", "migration"),
    "migration":    ("migration", "alembic"),
    "auth":         ("auth/", "session/", "login", "oauth", "jwt", "permission"),
    "login":        ("auth/", "login", "session/", "oauth"),
    "permission":   ("auth/", "rbac", "permission", "iam"),
    "session":      ("session/", "auth/"),
    "payment":      ("payment", "billing/", "stripe", "checkout", "invoice"),
    "billing":      ("billing/", "payment", "invoice", "subscription"),
    "checkout":     ("checkout", "payment", "billing/"),
    "stripe":       ("stripe", "payment", "billing/"),
    "webhook":      ("webhook", "callback", "events/"),
    "csv":          ("csv", "export", "report"),
    "export":       ("export", "report", "csv"),
    "encoding":     ("encoding", "unicode", "utf"),
    "unicode":      ("encoding", "unicode", "utf"),
    "latency":      ("performance", "perf", "cache"),
    "slow":         ("performance", "perf", "cache", "query"),
    "cache":        ("cache", "redis", "memcache"),
    "ratelimit":    ("ratelimit", "rate_limit", "throttle"),
    "rate":         ("ratelimit", "rate_limit", "throttle"),
    "email":        ("email", "mailer", "smtp", "notify"),
    "notification": ("notification", "notify", "email", "push"),
    "mobile":       ("mobile", "ios", "android"),
    "ios":          ("ios", "swift", "mobile"),
    "android":      ("android", "kotlin", "mobile"),
    "search":       ("search", "elastic", "index"),
    "upload":       ("upload", "storage/", "s3"),
    "image":        ("image", "media/", "upload"),
}


def _domain_path_substrings(description_tokens: set[str]) -> set[str]:
    out: set[str] = set()
    for tok in description_tokens:
        if tok in _DOMAIN_KEYWORDS:
            out.update(_DOMAIN_KEYWORDS[tok])
    return out


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@prs_server.tool
async def incident_to_pr(
    ctx: Context,
    repo: str,
    description: str,
    since: str | None = None,
    until: str | None = None,
    near_sha: str | None = None,
    window_hours: int = 12,
    candidate_pool: int = 50,
    top_n: int = 8,
) -> dict:
    """
    "Which PR caused this?" — rank merged PRs in a time window by how well
    they overlap with an incident description.

    Provide a window via either:
      - `since` (and optional `until`) as ISO-8601 dates/timestamps, OR
      - `near_sha`: a commit SHA that was running when the incident began.
        The window is computed as `[sha_time - window_hours, sha_time]`.

    Scoring per candidate PR:
      - 0.45 × path_token_match — overlap between description tokens and
        the path-segment tokens of the PR's touched files.
      - 0.30 × keyword_match — overlap between description tokens and the
        PR's title + body tokens.
      - 0.15 × recency_weight — newer PRs in the window rank higher.
      - 0.10 × signal_boost — PR touches paths in domains the description
        hints at (e.g. description mentions "database" → path matches `db/`).

    Returns the top N candidates, each with a per-signal breakdown so you
    can see WHY each PR ranked where it did. Pair with `find_related_prs`
    on the highest-scoring result to walk forward through related history.

    Use this when the user says "what shipped that broke X?", "which PR
    caused the latency spike at 3am?", or "when did this regression land?".
    """
    description = (description or "").strip()
    if not description:
        raise ValidationError("`description` is required.")

    client = await get_client(ctx)
    logger.info(
        "incident_to_pr(repo=%s, since=%s, near_sha=%s)",
        repo, since, near_sha,
    )

    # --- Resolve the window ---------------------------------------------
    if near_sha:
        try:
            commit = await client.get_commit(repo, near_sha)
        except Exception as exc:
            raise ValidationError(
                f"Could not resolve `near_sha` {near_sha!r} on {repo}: {exc}"
            ) from exc
        committer = ((commit.get("commit") or {}).get("committer") or {})
        sha_time = parse_iso(committer.get("date") or "")
        if sha_time is None:
            raise ValidationError(
                f"Commit {near_sha!r} has no committer timestamp."
            )
        until_dt = sha_time
        since_dt = sha_time - timedelta(hours=max(1, window_hours))
    else:
        until_dt = parse_date(until) if until else datetime.now(timezone.utc)
        since_dt = parse_date(since) if since else None
        if since_dt is None:
            raise ValidationError(
                "Provide `since` (or `near_sha`). `until` defaults to now."
            )
        if until_dt is None:
            raise ValidationError("Could not parse `until`.")
        if since_dt >= until_dt:
            raise ValidationError("`since` must be before `until`.")

    desc_tokens = _tokens(description)
    desc_path_tokens = desc_tokens
    domain_paths = _domain_path_substrings(desc_tokens)

    # --- Pull candidate PRs ---------------------------------------------
    query = (
        f"repo:{repo} type:pr is:merged "
        f"merged:{since_dt.date().isoformat()}..{until_dt.date().isoformat()}"
    )
    candidate_pool = max(10, min(candidate_pool, 200))
    max_pages = max(1, (candidate_pool + 99) // 100)
    await ctx.info(f"Searching: {query}")
    raw = await client.search_issues(query, max_pages=max_pages, per_page=100)
    raw = raw[:candidate_pool]

    if not raw:
        return {
            "repo": repo,
            "since": since_dt.isoformat(),
            "until": until_dt.isoformat(),
            "candidate_pool": 0,
            "results": [],
            "summary": (
                f"No merged PRs on {repo} between "
                f"{since_dt.date().isoformat()} and {until_dt.date().isoformat()}."
            ),
        }

    # --- Cheap-pass scoring on title+body, then file fetch on top half ---
    window_seconds = max((until_dt - since_dt).total_seconds(), 1.0)

    def _recency_weight(merged_at: str | None) -> float:
        t = parse_iso(merged_at or "")
        if not t:
            return 0.0
        # 1.0 at `until`, 0.0 at `since`. PRs merged after `until` (rare,
        # depends on date precision) cap at 1.0.
        offset = max((t - since_dt).total_seconds(), 0.0)
        return min(offset / window_seconds, 1.0)

    prelim: list[tuple[dict[str, Any], float, float]] = []
    for pr in raw:
        merged_at = (pr.get("pull_request") or {}).get("merged_at")
        recency = _recency_weight(merged_at)
        text_tokens = _tokens(f"{pr.get('title') or ''}\n{(pr.get('body') or '')[:1000]}")
        if desc_tokens and text_tokens:
            inter = len(desc_tokens & text_tokens)
            union = len(desc_tokens | text_tokens)
            keyword_score = inter / union if union else 0.0
        else:
            keyword_score = 0.0
        prelim.append((pr, keyword_score, recency))

    # Fetch files for the top ~K by (keyword + recency); cheap text/recency
    # ranks the full pool, expensive file fetches only happen on the head.
    prelim.sort(key=lambda t: t[1] + t[2], reverse=True)
    file_budget = prelim[: min(40, len(prelim))]

    sem = asyncio.Semaphore(6)

    async def _fetch_files(pr: dict[str, Any]) -> list[dict[str, Any]]:
        async with sem:
            try:
                full = pr["repository_url"].split("/repos/")[-1]
                return await client.get_pr_files(full, pr["number"])
            except Exception as exc:
                logger.debug("get_pr_files failed for #%s: %s", pr.get("number"), exc)
                return []

    file_lists = await asyncio.gather(*(_fetch_files(p) for p, _, _ in file_budget))
    files_by_number = {pr["number"]: fl for (pr, _, _), fl in zip(file_budget, file_lists)}

    # --- Final scoring ---------------------------------------------------
    scored: list[dict[str, Any]] = []
    for pr, keyword_score, recency in prelim:
        files = files_by_number.get(pr["number"], [])
        path_tokens: set[str] = set()
        path_strs: list[str] = []
        for f in files:
            name = f.get("filename") or ""
            path_strs.append(name)
            path_tokens.update(_path_tokens(name))

        if desc_path_tokens and path_tokens:
            inter = len(desc_path_tokens & path_tokens)
            # Use a one-sided ratio against description tokens — we're asking
            # "how much of the incident's vocabulary appears in this PR's
            # paths", not "how similar are these two sets".
            path_score = inter / max(len(desc_path_tokens), 1)
        else:
            path_score = 0.0

        signal_boost = 0.0
        matched_domains: list[str] = []
        if domain_paths:
            joined = " ".join(p.lower() for p in path_strs)
            matched_domains = [d for d in domain_paths if d in joined]
            if matched_domains:
                signal_boost = 1.0

        combined = (
            0.45 * path_score
            + 0.30 * keyword_score
            + 0.15 * recency
            + 0.10 * signal_boost
        )
        if combined <= 0:
            continue

        matched_paths = [
            p for p in path_strs
            if any(t in p.lower() for t in desc_path_tokens)
        ]

        scored.append({
            "number": pr["number"],
            "title": pr.get("title"),
            "url": pr.get("html_url"),
            "author": (pr.get("user") or {}).get("login"),
            "merged_at": (pr.get("pull_request") or {}).get("merged_at"),
            "score": round(combined, 3),
            "path_score": round(path_score, 3),
            "keyword_score": round(keyword_score, 3),
            "recency_weight": round(recency, 3),
            "domain_matches": matched_domains,
            "matched_paths": matched_paths[:6],
            "file_count": len(files),
        })

    scored.sort(key=lambda r: r["score"], reverse=True)
    top = scored[: max(1, top_n)]

    if not top:
        summary = (
            f"{len(raw)} merged PR(s) in window; none scored above zero on "
            "description overlap. Likely cause is outside this window or "
            "outside this repo."
        )
    else:
        best = top[0]
        summary = (
            f"Top candidate: #{best['number']} `{best['title']}` "
            f"(score {best['score']}, merged {best['merged_at']}). "
            f"Searched {len(raw)} merged PR(s) in window."
        )

    return {
        "repo": repo,
        "since": since_dt.isoformat(),
        "until": until_dt.isoformat(),
        "near_sha": near_sha,
        "description": description,
        "candidate_pool": len(raw),
        "files_fetched_for": len(file_budget),
        "results": top,
        "summary": summary,
    }
