# PRism: Autonomous GitHub MCP Server

![Python](https://img.shields.io/badge/Python-3.13+-blue.svg)
![MCP](https://img.shields.io/badge/Powered_by-Model_Context_Protocol-purple.svg)

PRism is an intelligent, agentic GitHub aggregation server designed natively for the **Model Context Protocol (MCP)**. It completely decouples complex GitHub workflows from rigid UIs by serving modular tools that can be dynamically wielded by AI orchestrators (like Claude Desktop or LangGraph agents).

## ✨ Features

The architecture is built progressively across three distinct phases of intelligence:

### Phase 1: Core Tooling
- `get_open_issues`: Fetches stateful issues filtered by branch labels or milestones.
- `get_my_prs`: Isolates open/merged PRs specifically assigned to or created by you.
- `get_pr_diff`: Extracts and truncates the raw unified patch of a PR.
- `list_repos`: Quickly fetches the authenticated user's repository ecosystem.

### Phase 2: The Intelligence Layer
- `review_pr`: Sends huge repository diffs to an LLM for bulleted summaries, risk flagging, and security analysis.
- `ci_status`: Tracks and verifies GitHub Actions workflow success configurations.

### Phase 3: Interactive & Agentic Workflows
- `comment_on_pr`: Seamlessly pushes code-review comments via MCP back to GitHub.
- `assign_reviewer`: **Blame-based auto-assignment.** Fetches the PR's most modified files, runs historical algorithmic commit-scoring, and assigns the highest-frequency historical domain contributors.
- `get_morning_briefing`: **The Multi-Repo Dashboard.** Uses `asyncio.gather` parallelization to aggregate awaiting reviews, authored PR statuses + CI build states, and assigned issues into a single, highly-readable Markdown digest.

## 🏗️ Architecture: The "Write Once, Run Anywhere" Principle

Because PRism is strictly built as an MCP Server, its tools are completely decoupled from its interface. You can interact with PRism in two ways:

1. **Claude Desktop**: Seamlessly load the server so Claude can review your code conversationally.
2. **Autonomous Agents**: Point independent a2a instances (like LangGraph or Autogen) directly at the server's STDIO to run 24/7 background PR reviews.

## 🚀 Getting Started

### Prerequisites
1. Python 3.13+
2. A GitHub Personal Access Token (`GH_PAT`)

### Installation
```bash
# Clone the repository
git clone https://github.com/your-username/PRism.git
cd PRism

# Create your environment file
echo "GH_PAT=ghp_your_secret_token" > .env

# Run dependency sync
uv sync
```

### Usage
#### Running as an MCP Server
To use PRism conversationally inside Claude Desktop, mount the server configuration inside your `claude_desktop_config.json`:
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

## 🛠️ Stack & Implementation Details
- **FastMCP**: Core dependency injection, Tool exposure, and server routing.
- **Httpx (Async)**: High-speed parallel I/O for `asyncio.gather` dashboard queries.
