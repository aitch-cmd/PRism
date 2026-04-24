# PRism: Autonomous GitHub MCP Server

![Python](https://img.shields.io/badge/Python-3.13+-blue.svg)
![MCP](https://img.shields.io/badge/Powered_by-Model_Context_Protocol-purple.svg)

PRism is an intelligent, agentic GitHub aggregation server built natively for the **Model Context Protocol (MCP)**. It decouples complex GitHub workflows from rigid UIs by exposing modular tools that can be driven conversationally by Claude or called directly by autonomous agents.

## ✨ Tools

### Browse & read
- `list_repos` — authenticated user's repositories.
- `get_open_issues` — issues filtered by branch label or milestone.
- `branch_tickets` — deep search for issues linked to a branch name.
- `get_my_prs` — cross-repo listing of PRs authored by or assigned to you, with CI + review state.
- `get_pr_diff` — raw unified diff, truncated to a line budget.
- `ci_status` — combined GitHub Actions check state for a PR.

### Review & comment
- `review_pr` — **stateful, incremental LLM review.** First call chunks and map-reduces the diff; subsequent calls on the same PR review only the new commits and flag NEW issues rather than re-listing old findings. Persisted per PR via the `PRReview` table.
- `comment_on_pr` — post a review comment back to GitHub.
- `assign_reviewer` — post-open reviewer assignment. Combines four signals (file-history blame, current open-PR load, recent turnaround time, OOO detection) to rank candidates, then auto-requests the top 2.

### Pre-flight & authoring (new)
- `suggest_reviewers_for_diff` — **pre-flight PR readiness**, runs BEFORE the PR is open. Analyses the branch against its base and returns ranked reviewers (no auto-assign), weighted estimated review minutes (code×1.0, tests×0.4, config×0.3 at ~300 lines/hr), a size category (tiny/small/medium/large/huge), and a split recommendation grouped by top-level module when the change is both large AND spread across ≥3 modules.
- `generate_pr_description` — drafts a structured PR body (**What / Why / Test Plan / Risk**) from the branch's commit messages + diff. Large diffs are chunked and map-reduced. Optional `post=True` finds the open PR for the branch and updates its body.
- `changelog_from_prs` — release changelog from merged PRs in a date range OR a milestone. Groups into Breaking → Security → Features → Bug Fixes → Performance → Refactors → Docs → Tests → Chores → Reverts → Other via conventional-commit titles first, then labels. Breaking changes are detected from `!:` in title, `breaking-change` label, OR `BREAKING CHANGE:` body trailer and called out in their own section. Returns both structured groupings and paste-ready Markdown.
- `find_related_prs` — given a PR or issue number, finds historically related PRs via three weighted signals: file overlap (Jaccard, 55%), text similarity on title + body (30%), and shared `#N` issue references (15%). Two-stage filtering keeps latency bounded: cheap text/ref scoring on the full pool, then file fetches only for the top ~40.

### Triage
- `triage_issues` — TF-IDF + cosine clustering over open issues with union-find. Flags likely duplicates above a configurable threshold, suggests labels from a canonical vocabulary via an LLM pass, and proposes stale issues for closure.

### Dashboard
- `get_morning_briefing` — multi-repo standup digest via parallel `asyncio.gather`: awaiting reviews, your PR statuses + CI, and assigned issues, rendered as one Markdown block.

## 🧱 Middleware

Every tool call flows through a composable middleware stack. Order matters — added first means outermost:

| Order | Middleware | Responsibility |
|---|---|---|
| 1 | `RequestIDMiddleware` | Stamps a request-id into context so every downstream log line carries it. |
| 2 | `ErrorHandlingMiddleware` | Catches and normalises exception shapes into structured errors. |
| 3 | `AuthMiddleware` | Resolves the caller via `Authorization: Bearer` / `X-GitHub-Token` / `GH_PAT` env, attaches a `GitHubClient` and `github_user` to context state. |
| 4 | `RateLimitMiddleware` | Per-authenticated-user rate limiting. |
| 5 | `IdempotencyMiddleware` | Collapses duplicate requests before opening a DB session. |
| 6 | `DatabaseSessionMiddleware` | Innermost. Opens a session around the tool body; commits on success, rolls back on raise. |

## 🏗️ Architecture

```
server.py
  ├── middleware/           (composable request pipeline — 6 layers above)
  ├── core/
  │   ├── db.py             (async SQLAlchemy + PRReview table for stateful reviews)
  │   ├── diff_chunker.py   (per-file diff splitter → review-sized chunks for map-reduce)
  │   ├── logger.py         (structured logging with request-id correlation)
  │   └── request_context.py
  ├── github_client.py      (thin async httpx wrapper — ALL REST calls live here;
  │                          swap PAT → OAuth by editing only this file)
  ├── tools/
  │   ├── repos.py          (list_repos)
  │   ├── issues.py         (get_open_issues, branch_tickets, triage_issues)
  │   ├── dashboard.py      (get_morning_briefing)
  │   └── prs/              (PR tooling package — split by concern)
  │       ├── __init__.py   (re-exports prs_server, triggers tool registration)
  │       ├── _server.py    (prs_server = FastMCP("prs") — isolated to avoid cycles)
  │       ├── _shared.py    (prompts, chunk-runner helpers, date parsers)
  │       ├── core.py       (get_my_prs, get_pr_diff, review_pr, ci_status, comment_on_pr)
  │       ├── reviewers.py  (assign_reviewer, suggest_reviewers_for_diff + scoring)
  │       └── authoring.py  (generate_pr_description, changelog_from_prs, find_related_prs)
  └── resources/user.py     (user:// MCP resource)
```

**"Write Once, Run Anywhere."** Because the tools are exposed as MCP primitives, the same server drives:

1. **Claude Desktop / Claude Code** — conversational code review and PR authoring.
2. **Autonomous agents** (LangGraph, Autogen, custom) — pointed at the server's HTTP/STDIO transport for 24/7 background triage and changelog generation.

## 🚀 Getting Started

### Prerequisites
1. Python 3.13+
2. A GitHub Personal Access Token (`GH_PAT`)

### Installation
```bash
git clone https://github.com/your-username/PRism.git
cd PRism

echo "GH_PAT=ghp_your_secret_token" > .env

uv sync
```

### Running the server
```bash
uv run python server.py
# listens on http://localhost:8000
```

### Claude Desktop config
```json
{
  "mcpServers": {
    "prism": {
      "command": "uv",
      "args": ["run", "python", "/absolute/path/to/PRism/server.py"]
    }
  }
}
```

## 🛠️ Stack
- **FastMCP** — tool registration, middleware pipeline, transport abstraction.
- **httpx (async)** — connection-pooled GitHub REST client with Link-header pagination.
- **SQLAlchemy (async)** — persistence for stateful tools (incremental review cache).
- **asyncio.gather** — parallel I/O for dashboard and chunked-diff map-reduce.
