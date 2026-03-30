---
name: "server-cli"
description: "CLI for the server MCP server. Call tools, list resources, and get prompts."
---

# server CLI

## Tool Commands

### authenticate

Authenticate with GitHub using a Personal Access Token (PAT).
Call this when the user wants to connect their GitHub account
or switch to a different token.
Returns the authenticated username on success.

```bash
uv run --with fastmcp python cli.py call-tool authenticate --token <value>
```

| Flag | Type | Required | Description |
|------|------|----------|-------------|
| `--token` | string | yes |  |

### list_repos

Lists all GitHub repositories accessible to the authenticated user.
Call this when the user asks about their repos, projects, or codebases.

```bash
uv run --with fastmcp python cli.py call-tool list_repos --sort <value> --repo-type <value> --limit <value>
```

| Flag | Type | Required | Description |
|------|------|----------|-------------|
| `--sort` | string | no |  |
| `--repo-type` | string | no |  |
| `--limit` | integer | no |  |

### get_open_issues

Get general open issues for a GitHub repository.

Call this when the user asks for a list of open bugs, tasks, or issues on a repo.
IMPORTANT: DO NOT use this tool if the user asks for issues linked to a specific git branch. 
If they mention a branch, use the `branch_tickets` tool instead.

```bash
uv run --with fastmcp python cli.py call-tool get_open_issues --repo <value> --branch <value> --milestone <value> --state <value> --limit <value>
```

| Flag | Type | Required | Description |
|------|------|----------|-------------|
| `--repo` | string | yes |  |
| `--branch` | string | no | JSON string |
| `--milestone` | string | no | JSON string |
| `--state` | string | no |  |
| `--limit` | integer | no |  |

### branch_tickets

Find specific tickets or issues that are linked to a given branch_name.

USE THIS TOOL (instead of get_open_issues) whenever the user asks about issues connected, 
linked, or related to a branch name. It performs a deep search across issue bodies and labels.

```bash
uv run --with fastmcp python cli.py call-tool branch_tickets --repo <value> --branch-name <value>
```

| Flag | Type | Required | Description |
|------|------|----------|-------------|
| `--repo` | string | yes |  |
| `--branch-name` | string | yes |  |

### get_my_prs

Get pull requests where the authenticated user is the author or assignee.
Searches across ALL repos — not limited to a single repo.

Call this when the user asks about their PRs, pull requests, or code reviews.
For example: "What PRs do I have open?", "Show my merged PRs".

```bash
uv run --with fastmcp python cli.py call-tool get_my_prs --state <value> --limit <value>
```

| Flag | Type | Required | Description |
|------|------|----------|-------------|
| `--state` | string | no |  |
| `--limit` | integer | no |  |

### get_pr_diff

Get the raw unified diff for a pull request.
Call this when the user asks to see what changes are in a PR,
or wants you to summarize or review a PR.
Returns the diff as a string (truncated if it exceeds max_lines).

```bash
uv run --with fastmcp python cli.py call-tool get_pr_diff --repo <value> --pr-number <value> --max-lines <value>
```

| Flag | Type | Required | Description |
|------|------|----------|-------------|
| `--repo` | string | yes |  |
| `--pr-number` | integer | yes |  |
| `--max-lines` | integer | no |  |

### review_pr

Review a pull request by analyzing its raw diff.
Returns a summary, risk flags, and suggested reviewers.

```bash
uv run --with fastmcp python cli.py call-tool review_pr --repo <value> --pr-number <value>
```

| Flag | Type | Required | Description |
|------|------|----------|-------------|
| `--repo` | string | yes |  |
| `--pr-number` | integer | yes |  |

### ci_status

Check the GitHub Actions CI run status for a Pull Request.
Returns a minimal combined state like 'success', 'pending', or 'failure'.

```bash
uv run --with fastmcp python cli.py call-tool ci_status --repo <value> --pr-number <value>
```

| Flag | Type | Required | Description |
|------|------|----------|-------------|
| `--repo` | string | yes |  |
| `--pr-number` | integer | yes |  |

### comment_on_pr

Post a review comment on a pull request.
Use this to provide feedback, ask questions, or summarize findings on a PR.

```bash
uv run --with fastmcp python cli.py call-tool comment_on_pr --repo <value> --pr-number <value> --body <value>
```

| Flag | Type | Required | Description |
|------|------|----------|-------------|
| `--repo` | string | yes |  |
| `--pr-number` | integer | yes |  |
| `--body` | string | yes |  |

### assign_reviewer

Automatically determine and assign the best reviewer(s) for a PR based on file commit history.

```bash
uv run --with fastmcp python cli.py call-tool assign_reviewer --repo <value> --pr-number <value>
```

| Flag | Type | Required | Description |
|------|------|----------|-------------|
| `--repo` | string | yes |  |
| `--pr-number` | integer | yes |  |

### get_morning_briefing

Get a multi-repo morning briefing aggregating items across all repositories.
Returns Open issues assigned to you, PRs waiting for your review, and the status of PRs you authored.
Call this when the user asks for a dashboard, summary, or morning briefing.

```bash
uv run --with fastmcp python cli.py call-tool get_morning_briefing
```

## Utility Commands

```bash
uv run --with fastmcp python cli.py list-tools
uv run --with fastmcp python cli.py list-resources
uv run --with fastmcp python cli.py read-resource <uri>
uv run --with fastmcp python cli.py list-prompts
uv run --with fastmcp python cli.py get-prompt <name> [key=value ...]
```
