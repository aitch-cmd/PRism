from __future__ import annotations
import asyncio
from fastmcp import FastMCP, Context
from core.logger import get_logger
from tools.auth import get_client

logger = get_logger("prism.tools.dashboard")

dashboard_server = FastMCP("dashboard")

@dashboard_server.tool
async def get_morning_briefing(ctx: Context) -> str:
    """
    Get a multi-repo morning briefing aggregating items across all repositories.
    Returns Open issues assigned to you, PRs waiting for your review, and the status of PRs you authored.
    Call this when the user asks for a dashboard, summary, or morning briefing.
    """
    client = await get_client(ctx)
    user = await ctx.get_state("github_user")
    if not user:
        me = await client.get_authenticated_user()
        user = me["login"]
        
    logger.info("Building morning briefing for %s", user)
    
    # 1. Define the 3 queries
    review_query = f"type:pr state:open review-requested:{user}"
    my_prs_query = f"type:pr state:open author:{user}"
    my_issues_query = f"type:issue state:open assignee:{user}"
    
    # 2. Run the 3 core queries in parallel
    reviews_req, my_prs_req, my_issues_req = await asyncio.gather(
        client.search_issues(review_query, max_pages=1, per_page=15),
        client.search_issues(my_prs_query, max_pages=1, per_page=15),
        client.search_issues(my_issues_query, max_pages=1, per_page=15),
        return_exceptions=True
    )
    
    # Handle possible exceptions from parallel calls gracefully
    reviews = reviews_req if isinstance(reviews_req, list) else []
    my_prs = my_prs_req if isinstance(my_prs_req, list) else []
    my_issues = my_issues_req if isinstance(my_issues_req, list) else []
    
    # 3. For my open PRs, fetch CI commit statuses so we know if they are failing
    async def _fetch_pr_ci(pr):
        try:
            repo = pr["repository_url"].split("/repos/")[-1]
            number = pr["number"]
            # get the HEAD sha
            pr_detail = await client.get_pr_detail(repo, number)
            sha = pr_detail.get("head", {}).get("sha")
            status = "unknown"
            if sha:
                status = await client.get_commit_status(repo, sha)
            return {"pr": pr, "repo": repo, "ci": status}
        except Exception as exc:
            logger.warning("Failed to fetch CI for PR %s: %s", pr.get("number"), exc)
            return {"pr": pr, "repo": pr.get("repository_url", "").split("/repos/")[-1], "ci": "error"}
            
    pr_ci_results = await asyncio.gather(*[_fetch_pr_ci(pr) for pr in my_prs])
    
    # 4. Format the Markdown output
    lines = [f"# 🌅 Morning Briefing for @{user}\n"]
    
    # Section A: Needs My Review
    lines.append("## 🔍 Needs Your Review")
    if not reviews:
        lines.append("*You're all caught up! No PRs are waiting for your review.*\n")
    for r in reviews:
        repo = r["repository_url"].split("/repos/")[-1]
        lines.append(f"- **{repo}** #{r['number']}: [{r['title']}]({r['html_url']})")
    if reviews:
        lines.append("\n")
    
    # Section B: My PRs (with CI)
    lines.append("## 💻 Your Open PRs")
    if not pr_ci_results:
        lines.append("*You have no open PRs right now.*\n")
    for result in pr_ci_results:
        pr = result["pr"]
        repo = result["repo"]
        ci = result["ci"]
        
        if ci == "success":
            status_emoji = "✅"
        elif ci == "pending":
            status_emoji = "⏳"
        elif ci in ("unknown", "error"):
            status_emoji = "➖"
        else:
            status_emoji = "❌"
            
        lines.append(f"- {status_emoji} **{repo}** #{pr['number']}: [{pr['title']}]({pr['html_url']}) _(CI: {ci})_")
    if pr_ci_results:
        lines.append("\n")
    
    # Section C: Assigned Issues
    lines.append("## 🚨 Open Assigned Issues")
    if not my_issues:
        lines.append("*No open issues are currently assigned to you!*\n")
    for i in my_issues:
        repo = i["repository_url"].split("/repos/")[-1]
        lines.append(f"- **{repo}** #{i['number']}: [{i['title']}]({i['html_url']})")
        
    return "\n".join(lines)
