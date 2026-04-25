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
- `review_pr` — **map-reduced LLM review.** Chunks the diff, reviews each chunk in parallel, and synthesises one report with a Summary, Risk Flags, and Suggested Reviewers section. Handles PRs over ~2k lines reliably.
- `comment_on_pr` — post a review comment back to GitHub.
- `assign_reviewer` — post-open reviewer assignment. Combines four signals (file-history blame, current open-PR load, recent turnaround time, OOO detection) to rank candidates, then auto-requests the top 2.

### Pre-flight & authoring (new)
- `suggest_reviewers_for_diff` — **pre-flight PR readiness**, runs BEFORE the PR is open. Analyses the branch against its base and returns ranked reviewers (no auto-assign), weighted estimated review minutes (code×1.0, tests×0.4, config×0.3 at ~300 lines/hr), a size category (tiny/small/medium/large/huge), and a split recommendation grouped by top-level module when the change is both large AND spread across ≥3 modules.
- `generate_pr_description` — drafts a structured PR body (**What / Why / Test Plan / Risk**) from the branch's commit messages + diff. Large diffs are chunked and map-reduced. Optional `post=True` finds the open PR for the branch and updates its body.
- `changelog_from_prs` — release changelog from merged PRs in a date range OR a milestone. Groups into Breaking → Security → Features → Bug Fixes → Performance → Refactors → Docs → Tests → Chores → Reverts → Other via conventional-commit titles first, then labels. Breaking changes are detected from `!:` in title, `breaking-change` label, OR `BREAKING CHANGE:` body trailer and called out in their own section. Returns both structured groupings and paste-ready Markdown.
- `find_related_prs` — given a PR or issue number, finds historically related PRs via three weighted signals: file overlap (Jaccard, 55%), text similarity on title + body (30%), and shared `#N` issue references (15%). Two-stage filtering keeps latency bounded: cheap text/ref scoring on the full pool, then file fetches only for the top ~40.
- `pr_risk_score` — **pre-merge "should this PR get extra scrutiny?"** Synthesises five cheap signals into a 0-10 score with the *reasons* listed: size bucket (tiny→huge, ≤2.5pt), sensitive paths (auth/payments/migrations/IAM/secrets/infra/CI, ≤2.5pt), test-to-code ratio (≤2pt — heavy code additions with no tests pull this up), public-surface removals (removed top-level symbols, deleted files, modified `.proto`/`.graphql`/openapi contracts, ≤2.5pt), and CI flake on the head SHA (0.5pt). Three GitHub calls, no LLM, no diff parsing — what a sharp tech lead does in their head when triaging a review queue.
- `incident_to_pr` — **"which PR caused this?"** Given an incident description and a time window (`since`/`until` OR `near_sha` + `window_hours`), ranks merged PRs by a 4-signal score: path-token match (45%), title/body keyword match (30%), recency-decayed weight (15%), and a domain-keyword boost (10%, e.g. description mentions `database` → boost paths matching `db/`/`models/`/`migration`). Returns top-N with per-signal breakdown so reviewers see *why* each PR ranked where it did. Read-only, GitHub-only, no LLM.

### Cross-repo (the wedge)
- `blast_radius` — **cross-repo blast radius analysis.** Given a PR or branch, walks the changed surface (removed/renamed top-level symbols in Python/JS/TS/Go/Java/Kotlin, removed-or-renamed source files, modified `.proto` / `.graphql` / `openapi.yaml` contracts) and runs scoped GitHub code searches across the rest of the org for downstream call sites. Each impact gets a score (`severity × downstream_files × (1 + 0.3 × repos)`) and the response carries an aggregated `risk_level` (`none`/`low`/`medium`/`high`/`critical`) so reviewers see breakage risk in *other* repos before merging. Generic symbol names are filtered out and concurrent searches are capped to stay inside GitHub's tight code-search rate window.

### Team metrics
- `team_health` — **shipping metrics over time.** Scopes to `repo` or `org` over a time window (default last 30 days) and aggregates: throughput (merged PRs + per-week rate), size shape (median, p90, tiny→huge bucket distribution), review latency (PR open → first non-author review, median + p90), revert rate (title prefix or `This reverts commit` body trailer), and CI flake rate (sampled per PR — same check name on the same head SHA with at least one `failure` followed by a `success`). Returns a top-N per-author rollup with PR count, median size, and median review latency. Turns PRism from a per-PR assistant into a "how is the team shipping" lens.

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
| 5 | `IdempotencyMiddleware` | Innermost. Collapses duplicate requests so retries are cheap. |

## 🏗️ Architecture

```
server.py
  ├── middleware/           (composable request pipeline — 5 layers above)
  ├── core/
  │   ├── diff_chunker.py   (per-file diff splitter → review-sized chunks for map-reduce)
  │   ├── logger.py         (structured logging with request-id correlation)
  │   └── request_context.py
  ├── github_client.py      (thin async httpx wrapper — ALL REST calls live here;
  │                          swap PAT → OAuth by editing only this file)
  ├── tools/
  │   ├── repos.py          (list_repos)
  │   ├── issues.py         (get_open_issues, branch_tickets, triage_issues)
  │   ├── dashboard.py      (get_morning_briefing)
  │   ├── team.py           (team_health — shipping metrics over time)
  │   └── prs/              (PR tooling package — split by concern)
  │       ├── __init__.py   (re-exports prs_server, triggers tool registration)
  │       ├── _server.py    (prs_server = FastMCP("prs") — isolated to avoid cycles)
  │       ├── _shared.py    (prompts, chunk-runner helpers, date parsers)
  │       ├── core.py       (get_my_prs, get_pr_diff, review_pr, ci_status, comment_on_pr)
  │       ├── reviewers.py  (assign_reviewer, suggest_reviewers_for_diff + scoring)
  │       ├── authoring.py  (generate_pr_description, changelog_from_prs, find_related_prs)
  │       ├── blast_radius.py (blast_radius — cross-repo downstream usage scan)
  │       ├── risk.py       (pr_risk_score — pre-merge triage signals)
  │       └── incident.py   (incident_to_pr — "which PR caused this?")
  └── resources/user.py     (user:// MCP resource)
```

**Stateless by design.** No database, no migrations — the only runtime
dependency is a GitHub token. Drop the binary on any host, set `GH_PAT`,
and you're done.

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

## ☁️ Deployment (Azure Container Apps)

PRism is deployed to **Azure Container Apps** with a fully automated CI/CD pipeline. Three Azure services, no long-lived secrets:

```
push to main → GitHub Actions (OIDC) → ACR (image) → ACA (replicas 1-3, sticky)
                                                              ▲
                                                              │ Bearer <user PAT>
                                                         Claude Desktop
```

### Pipeline ([.github/workflows/docker-publish.yml](.github/workflows/docker-publish.yml))
1. **OIDC federation** — GitHub Actions exchanges a short-lived JWT (pinned to `repo:<owner>/PRism:ref:refs/heads/main`) for an Azure access token. Zero secrets in GitHub; client / tenant / subscription IDs are stored as repo *variables*.
2. **Build & push** — `docker/build-push-action` tags the image as both `:latest` and `:sha-<short>` and pushes to `prismacr.azurecr.io/prism`.
3. **Deploy** — `az containerapp update --image …:sha-<short>` creates a new immutable revision; ACA shifts traffic and drains old connections for zero-downtime rollouts. Rollback is `az containerapp revision activate --revision <prev>`.

### Container App configuration
- **Multi-tenant auth** — the container holds **zero credentials**. Every caller passes their own `Authorization: Bearer <github-pat>`; `AuthMiddleware` validates once per session against GitHub's `/user` endpoint and caches the client on context state. Each user gets their own GitHub rate-limit budget and audit attribution.
- **Sticky sessions** — MCP HTTP transport is stateful, so cookie-based session affinity is enabled on the ingress. Without it, request N+1 from the same client could land on a different replica and the session would die.
- **Managed identity for ACR pull** — system-assigned MI with `AcrPull` role; no docker registry password stored anywhere.
- **Sizing** — 0.25 vCPU / 0.5 GiB, `min-replicas: 1` (cold starts break in-flight MCP sessions), `max-replicas: 3` (HTTP-concurrency autoscale).
- **Observability** — Log Analytics auto-provisioned with the ACA environment. Request-ID middleware stamps every request, so a single `request_id` traces end-to-end across `ContainerAppConsoleLogs_CL`.

### How users connect to the deployed instance
Users mint their own GitHub PAT and configure Claude Desktop:
```json
{
  "mcpServers": {
    "prism": {
      "url": "https://<your-fqdn>.azurecontainerapps.io/mcp/",
      "headers": { "Authorization": "Bearer ghp_their_own_token" }
    }
  }
}
```

## 🛠️ Stack
- **FastMCP** — tool registration, middleware pipeline, transport abstraction.
- **httpx (async)** — connection-pooled GitHub REST client with Link-header pagination.
- **asyncio.gather** — parallel I/O for dashboard, chunked-diff map-reduce, and cross-repo searches.
