"""Written-artifact tools: PR descriptions, changelogs, and related-PR discovery."""
from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from typing import Any

from fastmcp import Context

from core.logger import get_logger
from middleware.auth import get_client
from middleware.error_handling import ValidationError

from ._server import prs_server
from ._shared import (
    DESCRIPTION_MAX_DIFF_LINES,
    draft_description_from_chunks,
    parse_date,
)

logger = get_logger("prism.tools.prs.authoring")


# ---------------------------------------------------------------------------
# generate_pr_description
# ---------------------------------------------------------------------------

@prs_server.tool
async def generate_pr_description(
    ctx: Context,
    repo: str,
    branch: str,
    base: str | None = None,
    post: bool = False,
    max_commits: int = 50,
) -> dict:
    """
    Generate a structured PR description (What / Why / Test Plan / Risk) from
    a branch's commits and diff against its base.

    - `base` defaults to the repo's default branch.
    - If `post=True`, finds the open PR for this branch and replaces its body.
      If no PR exists yet, the description is returned without posting.
    - Large diffs are chunked and map-reduced so huge PRs still produce a
      coherent description.

    Use this when the user says "write a PR description", "draft PR body for
    branch X", or "update my PR description".
    """
    client = await get_client(ctx)
    logger.info(
        "generate_pr_description(repo=%s, branch=%s, base=%s, post=%s)",
        repo, branch, base, post,
    )

    if base is None:
        repo_info = await client.get_repo(repo)
        base = repo_info.get("default_branch") or "main"
        await ctx.info(f"Using default base branch: {base}")

    if base == branch:
        raise ValidationError("Base and head branches are the same — nothing to describe.")

    try:
        comparison = await client.compare_commits(repo, base, branch)
    except Exception as exc:
        raise ValidationError(
            f"Could not compare {base}...{branch} on {repo}: {exc}"
        ) from exc

    commits = comparison.get("commits", []) or []
    if not commits:
        return {
            "repo": repo,
            "branch": branch,
            "base": base,
            "description": "",
            "note": f"No commits on {branch} ahead of {base}.",
            "posted": False,
        }

    commits = commits[-max_commits:]
    commit_lines = []
    for c in commits:
        sha = (c.get("sha") or "")[:7]
        msg = ((c.get("commit") or {}).get("message") or "").splitlines()[0]
        commit_lines.append(f"- {sha} {msg}")
    commits_block = (
        f"## Commits ({len(commits)} ahead of {base})\n" + "\n".join(commit_lines)
    )

    diff_text = await client.compare_commits_diff(repo, base, branch)
    diff_lines = diff_text.splitlines()
    truncated = False
    if len(diff_lines) > DESCRIPTION_MAX_DIFF_LINES * 4:
        # Hard guard against absurd diffs — chunker still handles the rest.
        diff_text = "\n".join(diff_lines[: DESCRIPTION_MAX_DIFF_LINES * 4])
        truncated = True

    if not diff_text.strip():
        return {
            "repo": repo,
            "branch": branch,
            "base": base,
            "description": "",
            "note": "Diff between base and branch is empty.",
            "posted": False,
        }

    await ctx.info(
        f"Drafting description from {len(commits)} commit(s) "
        f"and {len(diff_lines)} diff line(s)..."
    )
    description = await draft_description_from_chunks(ctx, diff_text, commits_block)
    description = (description or "").strip()

    if truncated:
        description += (
            "\n\n> _Note: diff was very large and truncated for description "
            "drafting; double-check the PR for anything missed._"
        )

    posted = False
    pr_number: int | None = None
    pr_url: str | None = None
    if post:
        existing = await client.find_pr_for_branch(repo, branch)
        if existing is None:
            return {
                "repo": repo,
                "branch": branch,
                "base": base,
                "description": description,
                "posted": False,
                "note": (
                    f"No open PR found for {branch}. Open a PR first, then call "
                    "this tool again with post=True."
                ),
            }
        pr_number = existing["number"]
        pr_url = existing.get("html_url")
        await client.update_pr(repo, pr_number, body=description)
        posted = True
        logger.info("Updated PR body repo=%s pr=%d", repo, pr_number)

    return {
        "repo": repo,
        "branch": branch,
        "base": base,
        "description": description,
        "posted": posted,
        "pr_number": pr_number,
        "pr_url": pr_url,
    }


# ---------------------------------------------------------------------------
# changelog_from_prs
# ---------------------------------------------------------------------------

# Conventional-commit-ish parser: `feat(api): ...`, `fix: ...`, `fix!: ...`
_CC_RE = re.compile(
    r"^\s*(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?(?P<bang>!?):\s*(?P<subject>.+)$"
)

# Map commit-type → changelog section. Anything unrecognised falls to "other".
_TYPE_TO_CATEGORY = {
    "feat": "feat",
    "feature": "feat",
    "fix": "fix",
    "bug": "fix",
    "bugfix": "fix",
    "perf": "perf",
    "refactor": "refactor",
    "docs": "docs",
    "doc": "docs",
    "test": "test",
    "tests": "test",
    "chore": "chore",
    "build": "chore",
    "ci": "chore",
    "style": "chore",
    "revert": "revert",
}

_LABEL_TO_CATEGORY = {
    "bug": "fix",
    "fix": "fix",
    "regression": "fix",
    "feature": "feat",
    "enhancement": "feat",
    "performance": "perf",
    "perf": "perf",
    "refactor": "refactor",
    "documentation": "docs",
    "docs": "docs",
    "test": "test",
    "tests": "test",
    "chore": "chore",
    "ci": "chore",
    "build": "chore",
    "dependencies": "chore",
    "security": "security",
    "breaking-change": "breaking",
    "breaking": "breaking",
}

_CATEGORY_ORDER = ("breaking", "security", "feat", "fix", "perf", "refactor", "docs", "test", "chore", "revert", "other")

_CATEGORY_HEADINGS = {
    "breaking": "Breaking Changes",
    "security": "Security",
    "feat": "Features",
    "fix": "Bug Fixes",
    "perf": "Performance",
    "refactor": "Refactors",
    "docs": "Documentation",
    "test": "Tests",
    "chore": "Chores",
    "revert": "Reverts",
    "other": "Other",
}

_BREAKING_BODY_RE = re.compile(r"BREAKING[- ]CHANGE\s*:", re.IGNORECASE)


def _classify_pr(pr: dict[str, Any]) -> tuple[str, bool]:
    """Return (category, is_breaking) for a PR by looking at title + labels + body."""
    title = pr.get("title") or ""
    body = pr.get("body") or ""
    label_names = {
        (l.get("name") or "").lower()
        for l in (pr.get("labels") or [])
        if isinstance(l, dict)
    }

    is_breaking = False
    category: str | None = None

    m = _CC_RE.match(title)
    if m:
        ctype = (m.group("type") or "").lower()
        if m.group("bang"):
            is_breaking = True
        category = _TYPE_TO_CATEGORY.get(ctype)

    for label in label_names:
        if label in ("breaking-change", "breaking"):
            is_breaking = True
        if category is None and label in _LABEL_TO_CATEGORY:
            category = _LABEL_TO_CATEGORY[label]

    if _BREAKING_BODY_RE.search(body):
        is_breaking = True

    if category is None:
        category = "other"
    return category, is_breaking


def _clean_pr_subject(title: str) -> str:
    """Strip a conventional-commit prefix from a title for display."""
    m = _CC_RE.match(title)
    if m:
        return m.group("subject").strip()
    return title.strip()


@prs_server.tool
async def changelog_from_prs(
    ctx: Context,
    repo: str,
    since: str | None = None,
    until: str | None = None,
    milestone: str | None = None,
    limit: int = 200,
) -> dict:
    """
    Generate a release changelog from merged PRs.

    Supply EITHER a date range (`since`/`until`, ISO-8601 dates or timestamps)
    OR a `milestone` title. Milestone overrides dates when both are given.

    PRs are grouped into sections — Breaking / Security / Features / Bug Fixes /
    Performance / Refactors / Documentation / Tests / Chores / Reverts / Other —
    using conventional-commit titles first, then labels. Any PR with a `!:` in
    its title, a `breaking-change` label, or a `BREAKING CHANGE:` trailer in
    its body is called out in its own section at the top.

    Returns both the structured grouping and a pre-formatted Markdown string
    ready to paste into RELEASE_NOTES.md.
    """
    client = await get_client(ctx)
    logger.info(
        "changelog_from_prs(repo=%s, since=%s, until=%s, milestone=%s)",
        repo, since, until, milestone,
    )

    since_dt = parse_date(since) if since else None
    until_dt = parse_date(until) if until else None

    query_parts = [f"repo:{repo}", "type:pr", "is:merged"]
    milestone_title: str | None = None
    if milestone:
        query_parts.append(f'milestone:"{milestone}"')
        milestone_title = milestone
    else:
        if since_dt and until_dt:
            query_parts.append(
                f"merged:{since_dt.date().isoformat()}..{until_dt.date().isoformat()}"
            )
        elif since_dt:
            query_parts.append(f"merged:>={since_dt.date().isoformat()}")
        elif until_dt:
            query_parts.append(f"merged:<={until_dt.date().isoformat()}")
        else:
            raise ValidationError(
                "Provide `since` (and optionally `until`), or a `milestone` title."
            )

    query = " ".join(query_parts)
    limit = max(1, min(limit, 500))
    max_pages = max(1, (limit + 99) // 100)

    await ctx.info(f"Searching merged PRs: {query}")
    raw = await client.search_issues(query, max_pages=max_pages, per_page=100)
    raw = raw[:limit]

    if not raw:
        return {
            "repo": repo,
            "query": query,
            "count": 0,
            "categories": {},
            "markdown": f"# Changelog\n\n_No merged PRs matched `{query}`._",
        }

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    breaking_entries: list[dict[str, Any]] = []

    for pr in raw:
        category, is_breaking = _classify_pr(pr)
        entry = {
            "number": pr["number"],
            "title": pr.get("title", ""),
            "subject": _clean_pr_subject(pr.get("title", "")),
            "url": pr.get("html_url"),
            "author": (pr.get("user") or {}).get("login"),
            "merged_at": (pr.get("pull_request") or {}).get("merged_at"),
            "labels": [l.get("name") for l in pr.get("labels") or [] if isinstance(l, dict)],
            "category": category,
            "is_breaking": is_breaking,
        }
        if is_breaking:
            breaking_entries.append(entry)
        grouped[category].append(entry)

    # Render Markdown
    lines: list[str] = []
    header = "# Changelog"
    if milestone_title:
        header += f" — {milestone_title}"
    elif since_dt or until_dt:
        rng = (
            f"{since_dt.date().isoformat() if since_dt else '…'}"
            f" → {until_dt.date().isoformat() if until_dt else 'today'}"
        )
        header += f" — {rng}"
    lines.append(header)
    lines.append("")
    lines.append(f"_{len(raw)} merged PR(s)._")
    lines.append("")

    def _format_entry(e: dict[str, Any]) -> str:
        author = f" (@{e['author']})" if e.get("author") else ""
        return f"- {e['subject']} — #{e['number']}{author}"

    if breaking_entries:
        lines.append("## ⚠️ Breaking Changes")
        lines.extend(_format_entry(e) for e in breaking_entries)
        lines.append("")

    for cat in _CATEGORY_ORDER:
        if cat == "breaking":
            continue  # already rendered above
        entries = [e for e in grouped.get(cat, []) if not e["is_breaking"]]
        if not entries:
            continue
        lines.append(f"## {_CATEGORY_HEADINGS[cat]}")
        lines.extend(_format_entry(e) for e in entries)
        lines.append("")

    markdown = "\n".join(lines).rstrip() + "\n"

    categories_out = {
        cat: grouped.get(cat, [])
        for cat in _CATEGORY_ORDER
        if grouped.get(cat)
    }

    return {
        "repo": repo,
        "query": query,
        "count": len(raw),
        "breaking_count": len(breaking_entries),
        "categories": categories_out,
        "markdown": markdown,
    }


# ---------------------------------------------------------------------------
# find_related_prs
# ---------------------------------------------------------------------------

_ISSUE_REF_RE = re.compile(r"(?:^|[\s(])#(\d+)\b")
_URL_ISSUE_REF_RE = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/(?:issues|pull)/(\d+)"
)

_RELATED_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")

_RELATED_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "have", "has", "will", "that", "this",
    "into", "when", "where", "what", "why", "how", "are", "was", "were", "been",
    "being", "they", "them", "their", "there", "here", "also", "only", "just",
    "about", "some", "any", "each", "every", "other", "such", "very", "too",
    "use", "using", "used", "make", "made", "does", "did", "doing",
    "not", "but", "yet", "its", "our", "your", "his", "her",
})


def _related_tokens(text: str) -> set[str]:
    return {
        t.lower()
        for t in _RELATED_WORD_RE.findall(text or "")
        if t.lower() not in _RELATED_STOPWORDS
    }


def _extract_issue_refs(body: str) -> set[int]:
    refs: set[int] = set()
    if not body:
        return refs
    for m in _ISSUE_REF_RE.finditer(body):
        try:
            refs.add(int(m.group(1)))
        except ValueError:
            pass
    for m in _URL_ISSUE_REF_RE.finditer(body):
        try:
            refs.add(int(m.group(1)))
        except ValueError:
            pass
    return refs


@prs_server.tool
async def find_related_prs(
    ctx: Context,
    repo: str,
    number: int,
    candidate_pool: int = 100,
    top_n: int = 10,
) -> dict:
    """
    Given a PR or issue number, find historically related PRs in the same repo.

    Signals combined per candidate:
      1. File overlap — Jaccard similarity between files touched (PRs only).
      2. Text similarity — token-overlap score on title + first 500 chars of body.
      3. Shared issue links — references to the same `#N` issues.

    Returns the top N candidates with per-signal sub-scores so the caller can
    see WHY each PR is related. Use when the user asks "has anyone touched
    this before?", "find PRs related to #123", or "what past PRs fixed similar
    issues?".

    `candidate_pool` controls how many recent merged PRs are scored — larger
    pool catches older matches at the cost of latency.
    """
    client = await get_client(ctx)
    logger.info(
        "find_related_prs(repo=%s, number=%d, pool=%d)", repo, number, candidate_pool
    )

    # The target may be a PR or an issue — issues endpoint returns both.
    target = await client.get_issue(repo, number)
    is_pr = "pull_request" in target
    target_body = target.get("body") or ""
    target_title = target.get("title") or ""
    target_tokens = _related_tokens(f"{target_title}\n{target_body[:500]}")
    target_refs = _extract_issue_refs(target_body) | {number}

    target_files: set[str] = set()
    if is_pr:
        try:
            files = await client.get_pr_files(repo, number)
            target_files = {
                f.get("filename") for f in files if f.get("filename")
            }
        except Exception as exc:
            logger.warning("Could not fetch files for target PR: %s", exc)

    candidate_pool = max(10, min(candidate_pool, 300))
    max_pages = max(1, (candidate_pool + 99) // 100)
    await ctx.info(f"Scoring up to {candidate_pool} recent merged PRs...")
    candidates = await client.search_issues(
        f"repo:{repo} type:pr is:merged",
        max_pages=max_pages,
        per_page=100,
    )
    candidates = [c for c in candidates if c["number"] != number][:candidate_pool]

    # Only fetch files for PRs whose text/issue-ref signal is non-zero — file
    # fetches are the expensive step and most candidates won't pass the first filter.
    prelim: list[tuple[dict[str, Any], float, float]] = []
    for c in candidates:
        c_body = c.get("body") or ""
        c_title = c.get("title") or ""
        c_tokens = _related_tokens(f"{c_title}\n{c_body[:500]}")
        if target_tokens and c_tokens:
            inter = len(target_tokens & c_tokens)
            union = len(target_tokens | c_tokens)
            text_sim = inter / union if union else 0.0
        else:
            text_sim = 0.0

        c_refs = _extract_issue_refs(c_body) | {c["number"]}
        shared_refs = target_refs & c_refs - {c["number"], number}
        ref_score = min(len(shared_refs), 5) / 5.0

        if text_sim > 0.02 or shared_refs:
            prelim.append((c, text_sim, ref_score))

    # Limit file fetches to the best-looking N by combined text+ref score.
    prelim.sort(key=lambda x: x[1] + x[2], reverse=True)
    file_budget = prelim[: min(40, len(prelim))]

    scored: list[dict[str, Any]] = []
    if target_files:
        file_fetch_tasks = [
            client.get_pr_files(repo, c["number"]) for c, _, _ in file_budget
        ]
        file_results = await asyncio.gather(*file_fetch_tasks, return_exceptions=True)
    else:
        file_results = [[] for _ in file_budget]

    budget_by_number = {
        c["number"]: (text_sim, ref_score, file_res)
        for (c, text_sim, ref_score), file_res in zip(file_budget, file_results)
    }

    for c, text_sim, ref_score in prelim:
        files_result = budget_by_number.get(c["number"], (text_sim, ref_score, None))[2]
        if isinstance(files_result, Exception) or files_result is None:
            c_files: set[str] = set()
        else:
            c_files = {f.get("filename") for f in files_result if f.get("filename")}

        if target_files and c_files:
            inter = len(target_files & c_files)
            union = len(target_files | c_files)
            file_sim = inter / union if union else 0.0
        else:
            file_sim = 0.0

        # Weighted combined score: file overlap is strongest signal for PRs.
        combined = (file_sim * 0.55) + (text_sim * 0.3) + (ref_score * 0.15)
        if combined <= 0:
            continue

        shared_files = sorted(target_files & c_files) if target_files and c_files else []
        c_refs = _extract_issue_refs(c.get("body") or "")
        shared_refs = sorted(target_refs & c_refs - {c["number"], number})

        scored.append({
            "number": c["number"],
            "title": c.get("title"),
            "url": c.get("html_url"),
            "author": (c.get("user") or {}).get("login"),
            "merged_at": (c.get("pull_request") or {}).get("merged_at"),
            "score": round(combined, 3),
            "file_similarity": round(file_sim, 3),
            "text_similarity": round(text_sim, 3),
            "shared_issue_refs": shared_refs,
            "shared_files": shared_files[:8],
            "shared_file_count": len(shared_files),
        })

    scored.sort(key=lambda r: r["score"], reverse=True)
    top = scored[: max(1, top_n)]

    return {
        "repo": repo,
        "target": {
            "number": number,
            "is_pr": is_pr,
            "title": target_title,
            "file_count": len(target_files),
            "issue_refs": sorted(target_refs - {number}),
        },
        "candidates_scored": len(scored),
        "candidate_pool": len(candidates),
        "related": top,
    }
