import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from skillflow.bootstrap import create_demo
from skillflow.demo import DemoSession
from skillflow.harness import live_server


def test_real_stdio_initialize_list_call_and_unknown_tool(tmp_path):
    root = create_demo(tmp_path / "mcp")
    fixture = DemoSession(root)
    with live_server(root) as (url, _):

        async def scenario():
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "skillflow",
                    "mcp",
                    "--url",
                    url,
                    "--token-file",
                    str(root / "writer.token"),
                ],
            )
            async with (
                stdio_client(parameters) as (reader, writer),
                ClientSession(reader, writer) as client,
            ):
                initialization = await client.initialize()
                assert initialization.serverInfo.name == "skillflow-mcp"
                tools = await client.list_tools()
                assert "flow_status" in {t.name for t in tools.tools}
                assert "approval_submit" not in {t.name for t in tools.tools}
                result = await client.call_tool("flow_status", {"case_id": fixture.case_id})
                status = json.loads(result.content[0].text)
                assert not result.isError and status["result"]["required_total"] == 2
                forbidden = await client.call_tool("approval_submit", {})
                assert (
                    forbidden.isError
                    and json.loads(forbidden.content[0].text)["error"]["code"] == "FORBIDDEN"
                )
                bad = await client.call_tool(
                    "flow_check", {"case_id": fixture.case_id, "role": "operator"}
                )
                assert bad.isError
                return initialization.protocolVersion

        version = asyncio.run(scenario())
        assert version in ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
