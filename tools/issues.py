from __future__ import annotations
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

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


# ---------------------------------------------------------------------------
# Triage — cluster, de-dupe, label-suggest, stale-detect
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")

# Deliberately compact. TF-IDF already down-weights globally common terms;
# this list only strips grammar-glue words that would otherwise dominate tf.
_STOPWORDS = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "can", "her",
    "was", "one", "our", "out", "this", "that", "with", "from", "have",
    "has", "had", "will", "would", "could", "should", "been", "were", "they",
    "them", "their", "there", "here", "when", "what", "where", "why", "how",
    "who", "which", "into", "than", "then", "also", "only", "just", "about",
    "some", "any", "each", "every", "other", "such", "very", "too", "got",
    "get", "use", "using", "used", "doing", "does", "did", "being",
})

CANONICAL_LABELS = (
    "bug, feature, enhancement, documentation, performance, security, "
    "refactor, test, ui, ux, backend, frontend, infra, build, ci, dependencies, "
    "good-first-issue, needs-info, wontfix, duplicate, regression"
)

LABEL_SYSTEM_PROMPT = (
    "You are a senior triage engineer. For each issue below, suggest 1-3 concise "
    "GitHub labels drawn from this canonical vocabulary only:\n"
    f"  {CANONICAL_LABELS}\n\n"
    "Rules:\n"
    "- Only output labels from the vocabulary above. Do not invent new ones.\n"
    "- Be conservative: fewer, more confident labels beats a noisy list.\n"
    "- Reply with JSON ONLY, no preamble, matching this exact shape:\n"
    '  {"labels": [{"number": <int>, "labels": ["..."]}]}'
)


def _tokenize(text: str) -> list[str]:
    return [
        t.lower()
        for t in _WORD_RE.findall(text or "")
        if t.lower() not in _STOPWORDS
    ]


def _tf_idf_vectors(docs: list[str]) -> list[dict[str, float]]:
    """
    Sparse TF-IDF vectors, L2-normalised. Stand-in for a hosted embeddings
    call: deterministic, zero-dependency, and plenty good for the
    "is-this-another-rendering-of-the-same-bug" signal.
    """
    token_lists = [_tokenize(d) for d in docs]

    df: Counter[str] = Counter()
    for toks in token_lists:
        df.update(set(toks))

    n = max(len(docs), 1)
    idf = {t: math.log((n + 1) / (df_t + 1)) + 1.0 for t, df_t in df.items()}

    vectors: list[dict[str, float]] = []
    for toks in token_lists:
        if not toks:
            vectors.append({})
            continue
        tf = Counter(toks)
        max_tf = max(tf.values())
        raw = {t: (c / max_tf) * idf.get(t, 0.0) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in raw.values())) or 1.0
        vectors.append({t: v / norm for t, v in raw.items()})
    return vectors


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    # Iterate the smaller dict for speed.
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


def _cluster(
    vectors: list[dict[str, float]], threshold: float
) -> tuple[list[list[int]], list[tuple[int, int, float]]]:
    """
    Union-Find clustering over pairwise cosine similarity.
    Returns (clusters of >=2 members, all edges above threshold).
    """
    n = len(vectors)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    edges: list[tuple[int, int, float]] = []
    for i in range(n):
        for j in range(i + 1, n):
            sim = _cosine(vectors[i], vectors[j])
            if sim >= threshold:
                edges.append((i, j, sim))
                union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for idx in range(n):
        groups[find(idx)].append(idx)

    clusters = sorted(
        (g for g in groups.values() if len(g) >= 2),
        key=len,
        reverse=True,
    )
    edges.sort(key=lambda e: e[2], reverse=True)
    return clusters, edges


def _is_stale(issue: dict[str, Any], threshold_days: int) -> bool:
    updated = issue.get("updated_at") or issue.get("created_at")
    if not updated:
        return False
    try:
        t = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        return False
    age_days = (datetime.now(timezone.utc) - t).total_seconds() / 86400.0
    return age_days > threshold_days


async def _suggest_labels(
    ctx: Context, issues: list[dict[str, Any]]
) -> dict[int, list[str]]:
    if not issues:
        return {}

    payload = [
        {
            "number": i["number"],
            "title": i["title"],
            # Truncate bodies so one run fits comfortably in context.
            "body": (i.get("body") or "")[:500],
        }
        for i in issues
    ]

    result = await ctx.sample(
        messages=json.dumps({"issues": payload}, indent=2),
        system_prompt=LABEL_SYSTEM_PROMPT,
        max_tokens=2000,
    )

    text = (result.text or "").strip()
    # Defensive: the model sometimes wraps JSON in ```json fences.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Label-suggestion JSON parse failed: %s", exc)
        return {}

    out: dict[int, list[str]] = {}
    for entry in parsed.get("labels", []):
        num = entry.get("number")
        labels = entry.get("labels") or []
        if isinstance(num, int) and isinstance(labels, list):
            out[num] = [str(l) for l in labels]
    return out


@issues_server.tool
async def triage_issues(
    ctx: Context,
    repo: str,
    limit: int = 100,
    similarity_threshold: float = 0.55,
    duplicate_threshold: float = 0.80,
    stale_days: int = 60,
) -> dict:
    """
    Triage all open issues in a repo.

    Operations performed (read-only — nothing is posted to GitHub):
      1. Cluster issues by text similarity (TF-IDF + cosine, union-find).
      2. Flag likely duplicates (similarity >= duplicate_threshold).
      3. Suggest labels per issue against a canonical vocabulary (LLM).
      4. Propose stale issues for closure (updated > stale_days ago).

    Returns a structured report. Tune thresholds per repo:
      - Lower similarity_threshold catches more loose relationships.
      - Raise duplicate_threshold to avoid false positives.

    Use this when the user asks to "clean up", "triage", "find duplicates",
    "suggest labels", or "audit stale issues" on a repo.
    """
    client = await get_client(ctx)

    limit = max(1, min(limit, 300))
    max_pages = max(1, (limit + 99) // 100)
    logger.info(
        "triage_issues(repo=%s, limit=%d, sim=%.2f, dup=%.2f, stale_days=%d)",
        repo, limit, similarity_threshold, duplicate_threshold, stale_days,
    )

    raw = await client.get_issues(
        repo,
        state="open",
        max_pages=max_pages,
        per_page=min(limit, 100),
    )
    raw = [i for i in raw if "pull_request" not in i][:limit]

    if not raw:
        return {
            "repo": repo,
            "total": 0,
            "clusters": [],
            "duplicate_pairs": [],
            "suggested_labels": [],
            "stale_for_closure": [],
            "note": "No open issues found.",
        }

    await ctx.info(f"Triaging {len(raw)} open issues from {repo}...")

    docs = [f"{i['title']}\n\n{(i.get('body') or '')[:2000]}" for i in raw]
    vectors = _tf_idf_vectors(docs)

    clusters_idx, edges = _cluster(vectors, similarity_threshold)

    clusters = [
        {
            "size": len(cluster),
            "issues": [
                {
                    "number": raw[idx]["number"],
                    "title": raw[idx]["title"],
                    "url": raw[idx]["html_url"],
                }
                for idx in cluster
            ],
        }
        for cluster in clusters_idx
    ]

    duplicate_pairs = [
        {
            "a": raw[i]["number"],
            "b": raw[j]["number"],
            "similarity": round(s, 3),
            "a_title": raw[i]["title"],
            "b_title": raw[j]["title"],
            "a_url": raw[i]["html_url"],
            "b_url": raw[j]["html_url"],
        }
        for i, j, s in edges
        if s >= duplicate_threshold
    ]

    stale_for_closure = [
        {
            "number": i["number"],
            "title": i["title"],
            "url": i["html_url"],
            "last_updated": i.get("updated_at"),
        }
        for i in raw
        if _is_stale(i, stale_days)
    ]

    await ctx.info("Asking the model for label suggestions...")
    label_map = await _suggest_labels(ctx, raw)

    suggested_labels = [
        {
            "number": i["number"],
            "title": i["title"],
            "current_labels": [l["name"] for l in i.get("labels", [])],
            "suggested_labels": label_map.get(i["number"], []),
        }
        for i in raw
        if label_map.get(i["number"])
    ]

    logger.info(
        "triage done repo=%s clusters=%d dupes=%d stale=%d labeled=%d",
        repo, len(clusters), len(duplicate_pairs),
        len(stale_for_closure), len(suggested_labels),
    )

    return {
        "repo": repo,
        "total": len(raw),
        "clusters": clusters,
        "duplicate_pairs": duplicate_pairs,
        "suggested_labels": suggested_labels,
        "stale_for_closure": stale_for_closure,
        "thresholds": {
            "similarity": similarity_threshold,
            "duplicate": duplicate_threshold,
            "stale_days": stale_days,
        },
    }
