import asyncio
import os
from dotenv import load_dotenv
from fastmcp import Client
from server import mcp

load_dotenv()

async def test():
    async with Client(mcp) as client:
        tools = await client.list_tools()
        print("Tools:", [t.name for t in tools])

        # authenticate
        token = os.getenv("GH_PAT")
        result = await client.call_tool("authenticate", {"token": token})
        print("Auth:", result)

        # list repos
        result = await client.call_tool("list_repos", {"limit": 3})
        print("Repos:", result)

        # get open issues
        result = await client.call_tool("get_open_issues", {"repo": "aitch-cmd/PRism", "limit": 5})
        print("Issues:", result)

        # get my prs
        result = await client.call_tool("get_my_prs", {"state": "open", "limit": 5})
        print("PRs:", result)

        # get pr diff (might 404 if PR #1 doesn't exist on the repo, handle gracefully)
        try:
            result = await client.call_tool("get_pr_diff", {"repo": "aitch-cmd/PRism", "pr_number": 1, "max_lines": 10})
            print("Diff:", result)
        except Exception as e:
            print("Diff (Expected Error):", e)

        # get open issues with branch filter
        result = await client.call_tool("get_open_issues", {"repo": "aitch-cmd/PRism", "branch": "feat/payments", "limit": 5})
        print("Issues on branch:", result)

asyncio.run(test())