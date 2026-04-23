from __future__ import annotations
from collections import Counter
from typing import Literal
from fastmcp import FastMCP, Context
from core.logger import get_logger
from middleware.auth import get_client

logger = get_logger("prism.tools.prs")

prs_server = FastMCP("prs")

@prs_server.tool
async def get_my_prs(
    ctx: Context,
    state: Literal["open", "closed", "merged"] = "open",
    limit: int = 30,
) -> list[dict]:
    """
    Get pull requests where the authenticated user is the author or assignee.
    Searches across ALL repos — not limited to a single repo.

    Call this when the user asks about their PRs, pull requests, or code reviews.
    For example: "What PRs do I have open?", "Show my merged PRs".
    """
    client = await get_client(ctx)
    user = await ctx.get_state("github_user")
    if not user:
        me = await client.get_authenticated_user()
        user = me["login"]

    limit = max(1, min(limit, 100))
    logger.info("get_my_prs(state=%s, limit=%d, user=%s)", state, limit, user)

    # GitHub Search: type:pr + author or assignee
    # "merged" is not a GitHub PR state — it's closed + merged
    if state == "merged":
        query = f"type:pr author:{user} is:merged"
    else:
        query = f"type:pr author:{user} is:{state}"

    raw_prs = await client.search_issues(query, max_pages=1, per_page=limit)
    raw_prs = raw_prs[:limit]

    prs = []
    for pr in raw_prs:
        repo = pr["repository_url"].split("/repos/")[-1]
        number = pr["number"]

        # Fetch extra info for CI and Reviews
        pr_detail = await client.get_pr_detail(repo, number)
        sha = pr_detail["head"]["sha"]
        branch = pr_detail["head"]["ref"]

        ci_status = await client.get_commit_status(repo, sha)

        reviews = await client.get_pr_reviews(repo, number)
        review_states = [r["state"] for r in reviews]
        if "CHANGES_REQUESTED" in review_states:
            review_status = "CHANGES_REQUESTED"
        elif "APPROVED" in review_states:
            review_status = "APPROVED"
        else:
            review_status = "PENDING"

        prs.append({
            "number": number,
            "title": pr["title"],
            "repo": repo,
            "branch": branch,
            "state": "merged" if pr.get("pull_request", {}).get("merged_at") else pr["state"],
            "ci_status": ci_status,
            "review_status": review_status,
            "created_at": pr["created_at"],
            "html_url": pr["html_url"],
        })

    logger.info("Returning %d PRs", len(prs))
    return prs

@prs_server.tool
async def get_pr_diff(
    ctx: Context,
    repo: str,
    pr_number: int,
    max_lines: int = 500,
) -> str:
    """
    Get the raw unified diff for a pull request.
    Call this when the user asks to see what changes are in a PR,
    or wants you to summarize or review a PR.
    Returns the diff as a string (truncated if it exceeds max_lines).
    """
    client = await get_client(ctx)
    logger.info("get_pr_diff(repo=%s, pr_number=%d, max_lines=%d)", repo, pr_number, max_lines)

    try:
        diff_text = await client.get_pr_diff(repo, pr_number)
    except Exception as exc:
        logger.warning("Failed to fetch diff: %s", exc)
        return f"❌ Error fetching PR diff: {exc}"

    lines = diff_text.splitlines()
    if len(lines) > max_lines:
        logger.info("Truncating diff from %d lines to %d", len(lines), max_lines)
        lines = lines[:max_lines]
        lines.append(f"\n... [Diff truncated at {max_lines} lines] ...")

    return "\n".join(lines)

@prs_server.tool
async def review_pr(ctx: Context, repo: str, pr_number: int) -> str:
    """
    Review a pull request by analyzing its raw diff.
    Returns a summary, risk flags, and suggested reviewers.
    """
    client = await get_client(ctx)
    await ctx.info(f"Fetching diff for {repo}#{pr_number}...")
    
    try:
        diff_text = await client.get_pr_diff(repo, pr_number)
    except Exception as exc:
        logger.warning("Failed to fetch diff for review: %s", exc)
        return f"❌ Error fetching PR diff: {exc}"
        
    lines = diff_text.splitlines()
    if len(lines) > 1000:
        await ctx.info("Diff is very large. Truncating for review...")
        lines = lines[:1000]
        lines.append("\n... [Diff truncated due to length] ...")
    diff_text = "\n".join(lines)
    
    await ctx.info("Sending diff to Claude for review...")
    
    prompt = (
        "You are an expert code reviewer. Review the following GitHub Pull Request diff.\n"
        "Provide your review in three sections:\n"
        "1. **Summary:** A brief 2-3 bullet point summary of what changed.\n"
        "2. **Risk Flags:** Identify any security risks, performance issues, or bugs. If none, say so.\n"
        "3. **Suggested Reviewers:** Suggest types of engineers who should review this based on the files changed.\n"
    )
    
    result = await ctx.sample(
        messages=diff_text,
        system_prompt=prompt,
        max_tokens=1500
    )
    
    return result.text

@prs_server.tool
async def ci_status(ctx: Context, repo: str, pr_number: int) -> str:
    """
    Check the GitHub Actions CI run status for a Pull Request.
    Returns a minimal combined state like 'success', 'pending', or 'failure'.
    """
    client = await get_client(ctx)
    logger.info("ci_status(repo=%s, pr_number=%d)", repo, pr_number)
    
    try:
        pr_detail = await client.get_pr_detail(repo, pr_number)
        sha = pr_detail.get("head", {}).get("sha")
        if not sha:
            return "❌ Unable to find the head commit SHA for this PR."
        
        status = await client.get_commit_status(repo, sha)
        return status
    except Exception as exc:
        logger.warning("Failed to fetch CI status: %s", exc)
        return f"❌ Error fetching CI status: {exc}"

@prs_server.tool
async def comment_on_pr(ctx: Context, repo: str, pr_number: int, body: str) -> str:
    """
    Post a review comment on a pull request.
    Use this to provide feedback, ask questions, or summarize findings on a PR.
    """
    client = await get_client(ctx)
    logger.info("comment_on_pr(repo=%s, pr_number=%d)", repo, pr_number)
    
    try:
        result = await client.create_pr_comment(repo, pr_number, body)
        return f"✅ Successfully posted comment on PR #{pr_number}. URL: {result.get('html_url')}"
    except Exception as exc:
        logger.warning("Failed to post comment on PR: %s", exc)
        return f"❌ Error posting comment: {exc}"

@prs_server.tool
async def assign_reviewer(ctx: Context, repo: str, pr_number: int) -> str:
    """
    Automatically determine and assign the best reviewer(s) for a PR based on file commit history.
    """
    client = await get_client(ctx)
    logger.info("assign_reviewer(repo=%s, pr_number=%d)", repo, pr_number)
    
    try:
        # 1. Get PR details to exclude the author
        pr_detail = await client.get_pr_detail(repo, pr_number)
        pr_author = pr_detail.get("user", {}).get("login")
        if not pr_author:
            return "❌ Could not determine PR author."
            
        # 2. Get files modified in the PR
        files = await client.get_pr_files(repo, pr_number)
        if not files:
            return "ℹ️ No files modified in this PR, cannot determine reviewers."
            
        # Take the top 5 most modified files to save API calls
        files = sorted(files, key=lambda f: f.get("changes", 0), reverse=True)[:5]
        
        # 3. Tally committers across all these files
        committers_tally = Counter()
        for f in files:
            filename = f.get("filename")
            if not filename:
                continue
            history = await client.get_file_commit_history(repo, filename)
            for commit in history:
                author = commit.get("author") or {}
                login = author.get("login")
                # Exclude author and bot accounts
                if login and login != pr_author and "[bot]" not in login and login != "web-flow":
                    committers_tally[login] += 1
                    
        if not committers_tally:
            return "❌ Could not find any past contributors (other than the author) to assign."
            
        # 4. Pick top 1-2 reviewers
        top_reviewers = [user for user, count in committers_tally.most_common(2)]
        
        # 5. Assign them
        await client.request_pr_review(repo, pr_number, top_reviewers)
        
        return f"✅ Successfully assigned reviewer(s): {', '.join(top_reviewers)}"
        
    except Exception as exc:
        logger.warning("Failed to auto-assign reviewer: %s", exc)
        return f"❌ Error assigning reviewer: {exc}"


