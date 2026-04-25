"""
Pre-merge risk triage. Synthesises cheap signals you can compute from the PR
itself — size, sensitive paths, test/code ratio, removed public surface,
recent CI flakes — into a single 0-10 score with the reasons spelled out.

What a sharp tech lead does in their head when triaging a queue of PRs.
Read-only, three GitHub calls, no LLM, no diff parsing beyond what
`get_pr_files` already returns.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from fastmcp import Context

from core.logger import get_logger
from middleware.auth import get_client

from ._server import prs_server
from ._shared import flaky_check_names, is_config_file, is_test_file, size_bucket
from .blast_radius import (
    _extract_symbols,
    _file_language,
    _filter_symbols,
    _is_api_contract,
)

logger = get_logger("prism.tools.prs.risk")


# ---------------------------------------------------------------------------
# Sensitivity classifier
# ---------------------------------------------------------------------------

# Each entry: (category, weight, path-substring tests). All matched against
# the lower-cased filename. Weights chosen so a single hit in the highest-
# severity category (secrets/IAM) saturates the 2.5pt sensitivity budget.
_SENSITIVITY_RULES: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    ("secrets/IAM",   2.0, ("iam/", "secrets/", "/.aws/", "kms", "vault/", ".env", "credentials")),
    ("auth",          1.5, ("auth/", "/auth.", "session/", "login", "oauth", "jwt", "permission", "rbac/")),
    ("payments",      1.5, ("payment", "billing/", "stripe", "invoice/", "checkout", "subscription")),
    ("migrations",    1.5, ("migration", "/migrations/", "alembic/", "schema.sql", "schema.prisma")),
    ("infra/deploy",  1.0, ("dockerfile", "docker-compose", "k8s/", "kubernetes/", "terraform/", ".tf", "helm/", "ansible/")),
    ("ci",            0.5, (".github/workflows/", ".gitlab-ci", ".circleci/", "buildkite/")),
)


def _categorise_sensitivity(path: str) -> list[str]:
    p = path.lower()
    hits: list[str] = []
    for category, _, needles in _SENSITIVITY_RULES:
        if any(n in p for n in needles):
            hits.append(category)
    return hits


# ---------------------------------------------------------------------------
# Risk-level mapping
# ---------------------------------------------------------------------------

def _risk_level(score: float) -> str:
    if score >= 7.0:
        return "critical"
    if score >= 4.0:
        return "high"
    if score >= 2.0:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@prs_server.tool
async def pr_risk_score(
    ctx: Context,
    repo: str,
    pr_number: int,
) -> dict:
    """
    Compute a pre-merge risk score for a PR — a 0-10 number plus the reasons.

    Synthesises five cheap signals (no diff parsing beyond what `get_pr_files`
    returns, no LLM call):

      1. Size (tiny→huge buckets, up to 2.5 pts).
      2. Sensitive paths: auth/, payments/, migrations/, IAM/secrets,
         infra (dockerfile/k8s/terraform), CI workflows. Up to 2.5 pts.
      3. Test/code ratio: heavy code additions with no/low test additions
         pull this up. Up to 2.0 pts.
      4. Public-surface removals: removed top-level symbols (Py/JS/TS/Go/
         Java/Kotlin), deleted source files, modified API contracts (.proto
         /.graphql/openapi). Up to 2.5 pts.
      5. CI flake on the head SHA: same check name with both a failure and
         a later success. 0.5 pts.

    Returns the score, level (low/medium/high/critical), per-signal
    breakdown, and a flat list of `reasons` so the caller (Claude or a
    dashboard) can show the *why*. Use this to triage a review queue —
    "which PRs need extra scrutiny?" — without reading every diff.
    """
    client = await get_client(ctx)
    logger.info("pr_risk_score(repo=%s, pr=%d)", repo, pr_number)

    pr_detail = await client.get_pr_detail(repo, pr_number)
    files = await client.get_pr_files(repo, pr_number)
    if files is None:
        files = []

    head_sha = (pr_detail.get("head") or {}).get("sha")

    additions = int(pr_detail.get("additions") or 0)
    deletions = int(pr_detail.get("deletions") or 0)
    total_changes = additions + deletions
    file_count = len(files)

    reasons: list[str] = []
    breakdown: dict[str, Any] = {}

    # --- 1. Size ----------------------------------------------------------
    size_label, size_pts = size_bucket(total_changes, file_count)
    breakdown["size"] = {
        "category": size_label,
        "points": round(size_pts, 2),
        "files": file_count,
        "lines": total_changes,
    }
    if size_pts >= 1.5:
        reasons.append(
            f"PR size is {size_label} ({total_changes} lines / {file_count} files)"
        )

    # --- 2. Sensitive paths ----------------------------------------------
    category_hits: dict[str, list[str]] = defaultdict(list)
    for f in files:
        path = f.get("filename") or ""
        for cat in _categorise_sensitivity(path):
            category_hits[cat].append(path)

    sens_pts_raw = 0.0
    for cat, weight, _ in _SENSITIVITY_RULES:
        if cat in category_hits:
            sens_pts_raw += weight
    sens_pts = min(2.5, sens_pts_raw)
    breakdown["sensitive_paths"] = {
        "points": round(sens_pts, 2),
        "categories": {
            cat: {"file_count": len(paths), "samples": paths[:3]}
            for cat, paths in category_hits.items()
        },
    }
    if category_hits:
        reasons.append(
            "Touches sensitive paths: "
            + ", ".join(
                f"{cat} ({len(paths)})"
                for cat, paths in sorted(
                    category_hits.items(), key=lambda kv: -len(kv[1])
                )
            )
        )

    # --- 3. Test/code ratio ----------------------------------------------
    test_adds = 0
    code_adds = 0
    for f in files:
        path = f.get("filename") or ""
        adds = int(f.get("additions") or 0)
        if is_test_file(path):
            test_adds += adds
        elif not is_config_file(path):
            code_adds += adds

    ratio: float | None = (test_adds / code_adds) if code_adds else None
    if code_adds < 50:
        coverage_pts = 0.0
        coverage_note = "small code change — coverage signal not meaningful"
    elif test_adds == 0:
        coverage_pts = 2.0
        coverage_note = f"{code_adds} lines of code added, NO test additions"
        reasons.append(coverage_note)
    elif ratio is not None and ratio < 0.1:
        coverage_pts = 1.5
        coverage_note = f"{code_adds} lines code / {test_adds} lines tests (ratio {ratio:.2f})"
        reasons.append("Test additions look thin: " + coverage_note)
    elif ratio is not None and ratio < 0.2:
        coverage_pts = 1.0
        coverage_note = f"{code_adds} lines code / {test_adds} lines tests (ratio {ratio:.2f})"
        reasons.append("Test additions look thin: " + coverage_note)
    else:
        coverage_pts = 0.0
        coverage_note = (
            f"{code_adds} lines code / {test_adds} lines tests"
            + (f" (ratio {ratio:.2f})" if ratio is not None else "")
        )
    breakdown["test_coverage"] = {
        "points": round(coverage_pts, 2),
        "code_additions": code_adds,
        "test_additions": test_adds,
        "ratio": round(ratio, 2) if ratio is not None else None,
        "note": coverage_note,
    }

    # --- 4. Public-surface removals --------------------------------------
    removed_symbol_count = 0
    modified_contract_count = 0
    removed_file_count = 0
    sample_removed: list[str] = []
    for f in files:
        path = f.get("filename") or ""
        prev = f.get("previous_filename") or path
        status = f.get("status") or "modified"
        patch = f.get("patch") or ""

        if _is_api_contract(path) or _is_api_contract(prev):
            modified_contract_count += 1
            sample_removed.append(f"contract:{path}")
            continue

        if status == "removed":
            removed_file_count += 1
            sample_removed.append(f"removed-file:{path}")
            continue

        lang = _file_language(path)
        if not lang or not patch:
            continue
        removed, added = _extract_symbols(patch, lang)
        net_removed = _filter_symbols(removed - added)
        if net_removed:
            removed_symbol_count += len(net_removed)
            for sym in list(net_removed)[:3]:
                sample_removed.append(f"{sym} ({path})")

    surface_raw = (
        removed_symbol_count * 0.5
        + removed_file_count * 1.0
        + modified_contract_count * 0.75
    )
    surface_pts = min(2.5, surface_raw)
    breakdown["public_surface"] = {
        "points": round(surface_pts, 2),
        "removed_symbols": removed_symbol_count,
        "removed_files": removed_file_count,
        "modified_contracts": modified_contract_count,
        "samples": sample_removed[:6],
    }
    if surface_raw > 0:
        bits: list[str] = []
        if removed_symbol_count:
            bits.append(f"removes {removed_symbol_count} public symbol(s)")
        if removed_file_count:
            bits.append(f"deletes {removed_file_count} file(s)")
        if modified_contract_count:
            bits.append(f"modifies {modified_contract_count} API contract(s)")
        reasons.append("Breakage surface: " + "; ".join(bits))

    # --- 5. CI flake on head SHA -----------------------------------------
    flake_pts = 0.0
    flake_names: list[str] = []
    if head_sha:
        try:
            runs = await client.get_check_runs(repo, head_sha)
        except Exception as exc:
            logger.debug("check_runs failed for %s@%s: %s", repo, head_sha[:7], exc)
            runs = []
        flake_names = flaky_check_names(runs)
        if flake_names:
            flake_pts = 0.5
            reasons.append(
                f"CI flaked on head SHA: {', '.join(flake_names[:3])}"
                + (f" (+{len(flake_names) - 3} more)" if len(flake_names) > 3 else "")
            )
    breakdown["ci_flake"] = {
        "points": round(flake_pts, 2),
        "flaky_checks": flake_names,
    }

    # --- Total ------------------------------------------------------------
    score = round(min(10.0, size_pts + sens_pts + coverage_pts + surface_pts + flake_pts), 2)
    level = _risk_level(score)

    if not reasons:
        reasons.append("Routine PR — no notable risk signals.")

    return {
        "repo": repo,
        "pr_number": pr_number,
        "title": pr_detail.get("title"),
        "author": (pr_detail.get("user") or {}).get("login"),
        "score": score,
        "max_score": 10.0,
        "level": level,
        "reasons": reasons,
        "breakdown": breakdown,
        "stats": {
            "files": file_count,
            "additions": additions,
            "deletions": deletions,
            "head_sha": head_sha,
        },
    }
