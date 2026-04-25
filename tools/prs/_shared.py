"""Shared prompts, constants, and helpers used across the prs tool modules."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastmcp import Context

from core.diff_chunker import chunk_diff

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

DESCRIPTION_SYSTEM_PROMPT = (
    "You write pull request descriptions for senior engineers. You are given "
    "(a) the list of commit messages on the branch and (b) the full diff "
    "against the base branch.\n\n"
    "Return a PR body in GitHub-flavored Markdown with EXACTLY these four "
    "sections, in this order, and nothing else:\n\n"
    "## What\n"
    "2-4 bullets describing the concrete semantic changes — not a paraphrase "
    "of the commit log. Name the modules or behaviors that changed.\n\n"
    "## Why\n"
    "1-3 bullets on motivation, inferred from commit messages and the shape "
    "of the diff. If the motivation is not evident, write exactly: "
    "'Motivation not evident from the diff — author should fill in.' and stop.\n\n"
    "## Test Plan\n"
    "Bullets for how a reviewer should validate. Reference new/updated tests "
    "actually present in the diff with their file paths. Add manual-verification "
    "steps only for user-facing changes.\n\n"
    "## Risk\n"
    "Call out anything that warrants extra reviewer attention: migrations, "
    "breaking API changes, security-sensitive code, concurrency, external "
    "service calls, performance-sensitive paths. If genuinely trivial, write "
    "'Low — contained change.'\n\n"
    "No preamble. No sign-off. No emojis. Do not invent facts not in the input."
)

DESCRIPTION_CHUNK_SYSTEM_PROMPT = (
    "You are summarising ONE chunk of a larger pull request diff so another "
    "pass can assemble a PR description. Be factual and terse.\n\n"
    "Return bullets under two headings:\n"
    "- **Changes:** what this chunk does (1-3 bullets, name the symbols/files).\n"
    "- **Tests:** any test files added/modified in this chunk with their paths, or omit the heading if none.\n"
    "No preamble. Do not guess at motivation."
)

DESCRIPTION_MAX_DIFF_LINES = 1500


async def review_chunks(ctx: Context, diff_text: str) -> str:
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


async def draft_description_from_chunks(
    ctx: Context, diff_text: str, commits_block: str
) -> str:
    """Map-reduce over a big diff: per-chunk notes, then final synthesis."""
    chunks = chunk_diff(diff_text)
    if not chunks:
        return ""

    if len(chunks) == 1:
        result = await ctx.sample(
            messages=f"{commits_block}\n\n## Diff\n{chunks[0].text}",
            system_prompt=DESCRIPTION_SYSTEM_PROMPT,
            max_tokens=1200,
        )
        return result.text

    await ctx.info(f"Diff split into {len(chunks)} chunks. Summarising in parallel...")
    chunk_notes = await asyncio.gather(
        *(
            ctx.sample(
                messages=(
                    f"Files in this chunk: {', '.join(chunk.paths)}\n\n{chunk.text}"
                ),
                system_prompt=DESCRIPTION_CHUNK_SYSTEM_PROMPT,
                max_tokens=500,
            )
            for chunk in chunks
        )
    )

    bundled = "\n\n---\n\n".join(
        f"### Chunk {i + 1} — files: {', '.join(chunk.paths)}\n{note.text}"
        for i, (chunk, note) in enumerate(zip(chunks, chunk_notes))
    )
    synthesis = await ctx.sample(
        messages=(
            f"{commits_block}\n\n## Per-chunk notes\n{bundled}"
        ),
        system_prompt=DESCRIPTION_SYSTEM_PROMPT,
        max_tokens=1200,
    )
    return synthesis.text


def parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def hours_between(a: datetime, b: datetime) -> float:
    return abs((b - a).total_seconds()) / 3600.0


def parse_date(s: str) -> datetime | None:
    if not s:
        return None
    try:
        if "T" not in s and len(s) == 10:
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Path classification — used by review-time estimation, risk scoring, and
# anywhere else we want to weight tests/configs differently from code.
# ---------------------------------------------------------------------------

def is_test_file(path: str) -> bool:
    p = path.lower()
    return (
        "/test" in p
        or "/tests/" in p
        or p.startswith("test")
        or "/spec/" in p
        or "__tests__" in p
        or p.endswith((
            "_test.go", "_test.py", "_spec.rb",
            ".test.ts", ".test.tsx", ".test.js",
            ".spec.ts", ".spec.tsx", ".spec.js",
        ))
    )


def is_config_file(path: str) -> bool:
    p = path.lower()
    if "dockerfile" in p:
        return True
    return p.endswith((
        ".yml", ".yaml", ".toml", ".ini", ".cfg", ".lock", ".json",
        ".md", ".txt", ".env", ".gitignore",
    ))


# ---------------------------------------------------------------------------
# Size buckets — single source of truth for tiny/small/medium/large/huge.
# Reviewers tooling wants the label only; risk scoring wants both label and
# a points contribution, so the helper returns both.
# ---------------------------------------------------------------------------

def size_bucket(total_changes: int, file_count: int) -> tuple[str, float]:
    """Return (label, points). `points` contributes up to 2.5 to a 0-10 score."""
    if total_changes <= 50 and file_count <= 3:
        return "tiny", 0.0
    if total_changes <= 200 and file_count <= 8:
        return "small", 0.5
    if total_changes <= 500 and file_count <= 15:
        return "medium", 1.5
    if total_changes <= 1500 and file_count <= 40:
        return "large", 2.0
    return "huge", 2.5


# ---------------------------------------------------------------------------
# CI flake detection — same algorithm wherever check runs are inspected.
# ---------------------------------------------------------------------------

def flaky_check_names(runs: list[dict[str, Any]]) -> list[str]:
    """
    Group check-run records by `name`. A name is "flaky" when its history on
    a single SHA contains both a `failure` and a later `success` — i.e. the
    same job went red then green without the underlying code changing.

    Returns the list of flaky names (empty when none qualify).
    """
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        by_name[r.get("name") or ""].append(r)
    flaky: list[str] = []
    for name, rs in by_name.items():
        if len(rs) < 2:
            continue
        rs.sort(key=lambda r: r.get("started_at") or "")
        outcomes = [r.get("conclusion") for r in rs]
        if "failure" in outcomes and outcomes[-1] == "success":
            flaky.append(name)
    return flaky
