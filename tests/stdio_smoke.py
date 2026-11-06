"""End-to-end check over the real stdio transport: spawn `harnessdoc mcp`, list tools, call scan.

    .venv/bin/python tests/stdio_smoke.py examples/demo_app

Not collected by pytest (spawns a subprocess); run it before a release or in CI.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main(project: str) -> None:
    exe = Path(sys.executable).with_name("harnessdoc")
    params = StdioServerParameters(command=str(exe), args=["mcp"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        print("tools:", ", ".join(sorted(t.name for t in tools.tools)))
        result = await session.call_tool("scan", {"project_path": project, "min_severity": "high"})
        data = json.loads(result.content[0].text)
        print(f"scan over stdio: {len(data['call_sites'])} call sites, {len(data['findings'])} high findings")
        bad = await session.call_tool("explain_rule", {"rule": "HD999"})
        print("tool error surfaces as isError:", bad.is_error, "-", bad.content[0].text[:60])


if __name__ == "__main__":
    asyncio.run(main(str(Path(sys.argv[1]).resolve())))
