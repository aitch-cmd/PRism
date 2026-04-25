"""
Cross-repo blast radius analysis.

Most code-review tools see one repo at a time. When a PR modifies a shared
library, an API contract, or a public symbol, the real risk lives in the
*other* repos that depend on it. This tool walks the changed surface, then
runs targeted GitHub code searches across the rest of the org to find
downstream call sites — so a reviewer sees who else is going to break before
the change ships.
"""
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

logger = get_logger("prism.tools.prs.blast_radius")


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------

# Top-level Python definitions (no leading whitespace → not nested in a class/fn).
_PY_TOP_DEF_RE = re.compile(r"^def\s+([A-Za-z_]\w*)\s*\(")
_PY_TOP_CLASS_RE = re.compile(r"^class\s+([A-Za-z_]\w*)\b")
_PY_TOP_CONST_RE = re.compile(r"^([A-Z][A-Z0-9_]{2,})\s*[:=]")

# JS/TS exported declarations + named export blocks.
_JS_EXPORT_RE = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function\s*\*?|class|const|let|var|interface|type|enum)\s+"
    r"([A-Za-z_$][\w$]*)"
)
_JS_NAMED_EXPORT_RE = re.compile(r"export\s*\{\s*([^}]+)\s*\}")

# Go: exported identifiers are capitalised by convention.
_GO_FUNC_RE = re.compile(r"^func\s+(?:\([^)]*\)\s+)?([A-Z]\w*)")
_GO_TYPE_RE = re.compile(r"^type\s+([A-Z]\w*)")
_GO_VAR_RE = re.compile(r"^(?:var|const)\s+([A-Z]\w*)")

# Java/Kotlin: any `public` declaration is callable from outside the package.
_JAVA_PUBLIC_RE = re.compile(
    r"\bpublic\s+(?:static\s+)?(?:final\s+)?"
    r"(?:abstract\s+)?(?:class|interface|enum|record|@interface|\w+(?:<[^>]+>)?)\s+"
    r"([A-Za-z_]\w*)"
)


_LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".js": "js",
    ".jsx": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".ts": "ts",
    ".tsx": "ts",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
}


# Symbol names that are too generic to search for usefully — they'd return
# tens of thousands of false-positive hits in any non-trivial org.
_SYMBOL_BLACKLIST = frozenset({
    "init", "main", "test", "setup", "config", "data", "info",
    "error", "warn", "debug", "log", "logger",
    "user", "self", "that", "value", "result", "response", "request",
    "open", "close", "start", "stop", "run", "exec", "call",
    "name", "type", "kind", "size", "count", "index", "item", "items",
})

_MIN_SYMBOL_LEN = 4


def _ext(path: str) -> str:
    if "/" in path:
        path = path.rsplit("/", 1)[-1]
    if "." not in path:
        return ""
    return "." + path.rsplit(".", 1)[1].lower()


def _file_language(path: str) -> str | None:
    return _LANG_BY_EXT.get(_ext(path))


def _basename_no_ext(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] if "." in base else base


def _is_api_contract(path: str) -> bool:
    p = path.lower()
    if p.endswith((".proto", ".graphql", ".graphqls")):
        return True
    base = p.rsplit("/", 1)[-1]
    if base.startswith("openapi") or base.startswith("swagger"):
        return True
    if "openapi" in base and base.endswith((".yml", ".yaml", ".json")):
        return True
    return False


def _extract_symbols(patch: str, language: str) -> tuple[set[str], set[str]]:
    """
    Walk the unified-diff patch and pull symbol names off `+`/`-` lines.

    Returns (removed, added). The same name on both sides means the line was
    rewritten — likely a signature change rather than an outright removal.
    """
    removed: set[str] = set()
    added: set[str] = set()

    for raw in patch.splitlines():
        if not raw or raw.startswith(("@@", "+++", "---", "diff ", "index ")):
            continue
        sign = raw[0]
        if sign not in ("+", "-"):
            continue
        line = raw[1:]
        target = removed if sign == "-" else added

        if language == "python":
            for rx in (_PY_TOP_DEF_RE, _PY_TOP_CLASS_RE, _PY_TOP_CONST_RE):
                m = rx.match(line)
                if m:
                    target.add(m.group(1))
        elif language in ("js", "ts"):
            m = _JS_EXPORT_RE.match(line)
            if m:
                target.add(m.group(1))
            for m2 in _JS_NAMED_EXPORT_RE.finditer(line):
                for name in m2.group(1).split(","):
                    name = name.strip().split(" as ")[0].strip()
                    if name:
                        target.add(name)
        elif language == "go":
            for rx in (_GO_FUNC_RE, _GO_TYPE_RE, _GO_VAR_RE):
                m = rx.match(line)
                if m:
                    target.add(m.group(1))
        elif language in ("java", "kotlin"):
            m = _JAVA_PUBLIC_RE.search(line)
            if m:
                target.add(m.group(1))

    return removed, added


def _filter_symbols(symbols: set[str]) -> set[str]:
    return {
        s for s in symbols
        if s
        and len(s) >= _MIN_SYMBOL_LEN
        and not s.startswith("_")
        and s.lower() not in _SYMBOL_BLACKLIST
    }


# ---------------------------------------------------------------------------
# Search query builders
# ---------------------------------------------------------------------------

def _scope(org: str, repo: str) -> str:
    return f"org:{org} -repo:{repo}"


def _symbol_queries(
    symbol: str, language: str, org: str, repo: str
) -> list[str]:
    s = _scope(org, repo)
    q = f'"{symbol}("'
    if language == "python":
        return [
            f'{s} language:python {q}',
            f'{s} language:python "import {symbol}"',
        ]
    if language in ("js", "ts"):
        return [
            f'{s} language:typescript {q}',
            f'{s} language:javascript {q}',
        ]
    if language == "go":
        return [f'{s} language:go {q}']
    if language == "java":
        return [f'{s} language:java {q}']
    if language == "kotlin":
        return [f'{s} language:kotlin {q}']
    return [f"{s} {q}"]


def _file_queries(path: str, org: str, repo: str) -> list[str]:
    s = _scope(org, repo)
    queries: list[str] = []
    if path.endswith(".py"):
        module = _basename_no_ext(path)
        # Same-package import paths vary by repo layout, so keep these loose.
        queries.append(f'{s} language:python "from {module}"')
        queries.append(f'{s} language:python "import {module}"')
    base = _basename_no_ext(path)
    queries.append(f'{s} "{base}"')
    return queries[:3]


def _contract_queries(path: str, org: str, repo: str) -> list[str]:
    s = _scope(org, repo)
    p = path.lower()
    if p.endswith(".proto"):
        proto = path.rsplit("/", 1)[-1]
        return [
            f'{s} "import \\"{proto}\\""',
            f'{s} "{proto}"',
        ]
    return [f'{s} "{_basename_no_ext(path)}"']


# ---------------------------------------------------------------------------
# Risk model
# ---------------------------------------------------------------------------

def _risk_level(total: float, max_score: float, repo_count: int) -> str:
    if max_score >= 30 or repo_count >= 5:
        return "critical"
    if max_score >= 10 or repo_count >= 2:
        return "high"
    if total >= 1:
        return "medium"
    if total > 0:
        return "low"
    return "none"


def _impact_score(severity: float, files: int, repos: int) -> float:
    return severity * files * (1.0 + 0.3 * repos)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@prs_server.tool
async def blast_radius(
    ctx: Context,
    repo: str,
    pr_number: int | None = None,
    branch: str | None = None,
    base: str | None = None,
    org: str | None = None,
    max_impacts: int = 10,
    max_results_per_query: int = 30,
) -> dict:
    """
    Cross-repo blast radius analysis.

    Given a PR (or a branch + base), detects changes that may break OTHER
    repos in the same org and runs scoped GitHub code searches to find the
    downstream call sites:

      - Removed or renamed top-level symbols (Python def/class/CONST,
        JS/TS `export`, Go capitalised funcs/types, Java/Kotlin `public`).
      - Removed or renamed source files (downstream `from X import …`).
      - Modified API contracts (.proto, .graphql/.graphqls, openapi.yaml).

    Each impact is scored by `severity × downstream_files × (1 + 0.3 × repos)`
    and the response carries an overall `risk_level` so reviewers know what
    to look at first. Rate-limit conscious: caps queries per impact, runs at
    most 3 concurrent searches, and skips symbols too generic to search for.

    Use this when the user says "what does this PR break?", "who else uses
    this function?", or "is this safe to merge in shared-lib?".
    """
    client = await get_client(ctx)
    logger.info(
        "blast_radius(repo=%s, pr=%s, branch=%s)", repo, pr_number, branch
    )

    if pr_number is None and branch is None:
        raise ValidationError("Provide either `pr_number` or `branch`.")
    if pr_number is not None and branch is not None:
        raise ValidationError(
            "Provide `pr_number` OR `branch`, not both — they're alternatives."
        )

    if pr_number is not None:
        try:
            files = await client.get_pr_files(repo, pr_number)
        except Exception as exc:
            raise ValidationError(
                f"Could not fetch files for {repo}#{pr_number}: {exc}"
            ) from exc
    else:
        if base is None:
            repo_info = await client.get_repo(repo)
            base = repo_info.get("default_branch") or "main"
        if base == branch:
            raise ValidationError("Base and head branches are the same.")
        try:
            comparison = await client.compare_commits(repo, base, branch)
        except Exception as exc:
            raise ValidationError(
                f"Could not compare {base}...{branch} on {repo}: {exc}"
            ) from exc
        files = comparison.get("files") or []

    if org is None:
        org = repo.split("/", 1)[0]

    if not files:
        return {
            "repo": repo,
            "pr_number": pr_number,
            "branch": branch,
            "base": base,
            "org": org,
            "summary": "No file changes — no blast radius.",
            "risk_level": "none",
            "risk_score": 0.0,
            "downstream_repo_count": 0,
            "affected_repos": [],
            "impacts": [],
        }

    impacts: list[dict[str, Any]] = []

    for f in files:
        path = f.get("filename") or ""
        prev = f.get("previous_filename") or path
        status = f.get("status") or "modified"
        patch = f.get("patch") or ""

        if _is_api_contract(path) or _is_api_contract(prev):
            impacts.append({
                "kind": "api_contract",
                "symbol": path,
                "language": "contract",
                "file": path,
                "previous_file": prev if prev != path else None,
                "status": status,
                "severity_label": "modified" if status == "modified" else status,
                "severity": 1.0 if status == "removed" else 0.7,
                "search_queries": _contract_queries(prev or path, org, repo),
            })
            continue

        if status == "removed":
            impacts.append({
                "kind": "removed_file",
                "symbol": _basename_no_ext(path),
                "language": _file_language(path) or "any",
                "file": path,
                "previous_file": None,
                "status": "removed",
                "severity_label": "removed",
                "severity": 1.0,
                "search_queries": _file_queries(path, org, repo),
            })
            continue

        if status == "renamed" and prev != path:
            impacts.append({
                "kind": "renamed_file",
                "symbol": _basename_no_ext(prev),
                "language": _file_language(prev) or "any",
                "file": path,
                "previous_file": prev,
                "status": "renamed",
                "severity_label": "renamed",
                "severity": 0.8,
                "search_queries": _file_queries(prev, org, repo),
            })
            # Fall through — symbol changes inside a renamed file still count.

        language = _file_language(path)
        if not language or not patch:
            continue

        removed, added = _extract_symbols(patch, language)
        net_removed = _filter_symbols(removed - added)
        modified = _filter_symbols(removed & added)

        for sym in net_removed:
            impacts.append({
                "kind": "removed_symbol",
                "symbol": sym,
                "language": language,
                "file": path,
                "previous_file": None,
                "status": "removed_or_renamed",
                "severity_label": "removed",
                "severity": 1.0,
                "search_queries": _symbol_queries(sym, language, org, repo),
            })
        for sym in modified:
            impacts.append({
                "kind": "modified_symbol",
                "symbol": sym,
                "language": language,
                "file": path,
                "previous_file": None,
                "status": "signature_changed",
                "severity_label": "modified",
                "severity": 0.5,
                "search_queries": _symbol_queries(sym, language, org, repo),
            })

    # Deduplicate by (kind, symbol, language) — same removed function can show
    # up across multiple hunks of the same file.
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for imp in impacts:
        key = (imp["kind"], imp["symbol"], imp["language"])
        if key not in seen:
            seen[key] = imp
    impacts = list(seen.values())

    if not impacts:
        return {
            "repo": repo,
            "pr_number": pr_number,
            "branch": branch,
            "base": base,
            "org": org,
            "summary": (
                "No public-surface changes detected — diff is internal "
                "implementation only. No cross-repo search performed."
            ),
            "risk_level": "low",
            "risk_score": 0.0,
            "downstream_repo_count": 0,
            "affected_repos": [],
            "impacts": [],
        }

    impacts.sort(key=lambda i: i["severity"], reverse=True)
    impacts = impacts[: max(1, max_impacts)]

    await ctx.info(
        f"Searching org `{org}` for downstream usages of {len(impacts)} "
        f"impact candidate(s)..."
    )

    # GitHub code search has a tight rate-limit window — keep parallelism low
    # and the per-query result cap small.
    sem = asyncio.Semaphore(3)

    async def _resolve(impact: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            usages_by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
            queries_run = 0
            for q in impact["search_queries"][:3]:
                queries_run += 1
                try:
                    items = await client.search_code(
                        q, max_results=max_results_per_query
                    )
                except Exception as exc:
                    logger.warning("code search failed query=%r: %s", q, exc)
                    continue
                for it in items:
                    full = (it.get("repository") or {}).get("full_name")
                    if not full or full == repo:
                        continue
                    usages_by_repo[full].append({
                        "path": it.get("path"),
                        "url": it.get("html_url"),
                    })

            unique_files = sum(
                len({u["path"] for u in uses})
                for uses in usages_by_repo.values()
            )
            unique_repos = len(usages_by_repo)
            score = _impact_score(impact["severity"], unique_files, unique_repos)

            sorted_repos = sorted(
                usages_by_repo.items(),
                key=lambda kv: len({u["path"] for u in kv[1]}),
                reverse=True,
            )
            return {
                **impact,
                "downstream_repos": [
                    {
                        "repo": r,
                        "file_count": len({u["path"] for u in uses}),
                        "samples": uses[:5],
                    }
                    for r, uses in sorted_repos
                ],
                "downstream_repo_count": unique_repos,
                "downstream_file_count": unique_files,
                "queries_run": queries_run,
                "score": round(score, 2),
            }

    resolved = await asyncio.gather(*(_resolve(i) for i in impacts))
    resolved.sort(key=lambda r: r["score"], reverse=True)

    affected_repos = sorted({
        ru["repo"] for r in resolved for ru in r["downstream_repos"]
    })
    total_score = sum(r["score"] for r in resolved)
    max_score = max((r["score"] for r in resolved), default=0.0)
    risk_level = _risk_level(total_score, max_score, len(affected_repos))

    if not affected_repos:
        summary = (
            f"Examined {len(resolved)} changed surface(s). No downstream "
            f"usages found in `{org}`. Local change."
        )
    else:
        top = next((r for r in resolved if r["downstream_repo_count"]), resolved[0])
        summary = (
            f"{len(affected_repos)} downstream repo(s) reference "
            f"{sum(1 for r in resolved if r['downstream_repo_count'])} of "
            f"{len(resolved)} changed surface(s). "
            f"Highest impact: {top['kind']} `{top['symbol']}` "
            f"({top['downstream_repo_count']} repo, "
            f"{top['downstream_file_count']} file)."
        )

    return {
        "repo": repo,
        "pr_number": pr_number,
        "branch": branch,
        "base": base,
        "org": org,
        "risk_level": risk_level,
        "risk_score": round(total_score, 2),
        "downstream_repo_count": len(affected_repos),
        "affected_repos": affected_repos,
        "impacts": resolved,
        "summary": summary,
    }
