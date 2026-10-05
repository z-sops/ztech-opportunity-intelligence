"""MCP adapter tests (spec #28, #29, #38 'MCP tests'): in-process client + real stdio subprocess."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from conftest import PROSPECT, make_engine
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from ztech_oi.adapters.mcp_server import create_server

EXPECTED_TOOLS = {
    "analyze_prospect_opportunity",
    "research_company",
    "discover_competitors",
    "research_competitors",
    "analyze_opportunity",
    "get_opportunity_report",
    "get_opportunity_timeline",
    "get_engine_info",
}
FORBIDDEN = ("sql", "query_db", "exec", "shell", "file", "read_path", "credential", "secret", "send", "email", "whatsapp")


@pytest.fixture
def server(market, search_market):
    return create_server(lambda: make_engine(market, search=search_market))


async def test_tool_surface_is_small_and_safe(server):
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == EXPECTED_TOOLS
    for name in tools:
        assert not any(f in name for f in FORBIDDEN)
    schema = tools["analyze_prospect_opportunity"].input_schema
    assert "prospect" in schema["properties"] and "prospect" in schema.get("required", [])


async def test_full_flow_over_mcp(server):
    async with Client(server) as c:
        r = await c.call_tool("analyze_prospect_opportunity", {"prospect": PROSPECT})
        assert not r.is_error
        summary = r.structured_content
        assert summary["schema_version"] == "1.0" and summary["opportunities"]
        rid = summary["research_id"]

        full = (await c.call_tool("get_opportunity_report", {"research_id": rid})).structured_content
        assert full["research_id"] == rid and full["evidence"]
        part = (
            await c.call_tool("get_opportunity_report", {"research_id": rid, "sections": ["sales_angles"]})
        ).structured_content
        assert set(part) == {
            "schema_version",
            "research_id",
            "status",
            "generated_at",
            "snapshot_id",
            "previous_snapshot_id",
            "sales_angles",
        }

        ana = (await c.call_tool("analyze_opportunity", {"research_id": rid})).structured_content
        assert ana["opportunity_score"]["score"] == summary["opportunity_score"]["score"]
        tl = (await c.call_tool("get_opportunity_timeline", {"domain": "sweetcrumb.com"})).structured_content
        assert tl["research_ids"] == [rid]
        disc = (await c.call_tool("discover_competitors", {"prospect": PROSPECT, "max_competitors": 1})).structured_content
        assert len(disc["competitors"]) == 1
        comp = await c.call_tool(
            "research_competitors",
            {"prospect": PROSPECT, "competitors": [{"company_name": "Cake House", "domain": "cakehouse.com"}]},
        )
        assert [x["domain"] for x in comp.structured_content["competitors"]] == ["cakehouse.com"]
        info = (await c.call_tool("get_engine_info", {})).structured_content
        assert info["schema_version"] == "1.0" and "confidence_producers" in info
        assert "key" not in json.dumps(info["configuration"]).lower() or "SECRET" not in json.dumps(info)


@pytest.mark.parametrize(
    "tool,args,code",
    [
        ("analyze_prospect_opportunity", {"prospect": {"company_name": "x", "domain": "localhost"}}, "INVALID_DOMAIN"),
        ("analyze_prospect_opportunity", {"prospect": {"company_name": ""}}, "company_name"),
        (
            "analyze_prospect_opportunity",
            {"prospect": {"company_name": "x"}, "options": {"max_competitors": 99}},
            "max_competitors",
        ),
        ("get_opportunity_report", {"research_id": "res_does_not_exist"}, "NOT_FOUND"),
        ("get_opportunity_report", {"research_id": "../../etc/passwd"}, "research_id"),
        ("get_opportunity_report", {"research_id": "res_x_1", "sections": ["drop table"]}, "VALIDATION_ERROR"),
        ("get_opportunity_timeline", {}, "VALIDATION_ERROR"),
        ("research_competitors", {"prospect": PROSPECT, "competitors": []}, "competitors"),
    ],
)
async def test_mcp_inputs_are_validated(server, tool, args, code):
    async with Client(server) as c:
        r = await c.call_tool(tool, args)
    assert r.is_error and code in r.content[0].text


async def test_stdio_transport_subprocess(tmp_path):
    """Launch the real console entry point over stdio (no network needed for these tools)."""
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": src, "ZTECH_OI_DB_PATH": str(tmp_path / "stdio.db"), "META_ACCESS_TOKEN": "TOPSECRET"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "ztech_oi.adapters.mcp_server"], env=env)
    async with Client(params) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == EXPECTED_TOOLS
        info = await c.call_tool("get_engine_info", {})
        assert not info.is_error and info.structured_content["configuration"]["meta_ad_library"]["configured"] is True
        assert "TOPSECRET" not in json.dumps(info.structured_content)
        nf = await c.call_tool("get_opportunity_report", {"research_id": "res_none_here"})
        assert nf.is_error and "NOT_FOUND" in nf.content[0].text
