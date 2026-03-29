import asyncio
import os
from dotenv import load_dotenv
from fastmcp import Client
from server import mcp

load_dotenv()

async def test():
    async with Client(mcp) as client:
        # list all available tools
        tools = await client.list_tools()
        print("Tools:", [t.name for t in tools])

        # authenticate first — list_repos needs this
        token = os.getenv("GITHUB_PAT")
        result = await client.call_tool("authenticate", {"token": token})
        print("Auth:", result)

        # now list_repos has a client in session state
        result = await client.call_tool("list_repos", {"limit": 5})
        print("Repos:", result)

asyncio.run(test())