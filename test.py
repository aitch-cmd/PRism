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
        token = os.getenv("GITHUB_PAT")
        result = await client.call_tool("authenticate", {"token": token})
        print("Auth:", result)

        # list repos
        result = await client.call_tool("list_repos", {"limit": 3})
        print("Repos:", result)

        # get open issues
        result = await client.call_tool("get_open_issues", {"repo": "aitch-cmd/PRism", "limit": 5})
        print("Issues:", result)

asyncio.run(test())