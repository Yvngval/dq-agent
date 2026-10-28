import asyncio
import json


def test_mcp_tools_are_read_only_views(warehouse):
    from dq_agent.mcp_server import server

    async def scenario():
        names = {t.name for t in await server.list_tools()}
        contract = await server.call_tool("source_contract", {})
        results = await server.call_tool("test_results", {})
        return names, contract, results

    names, contract, results = asyncio.run(scenario())
    assert {"list_tables", "describe_table", "test_results", "failing_rows", "profile_findings"} <= names
    assert json.loads(contract.content[0].text)["raw.flights"]["missing"] == []
    assert json.loads(results.content[0].text)["failures"] == []
