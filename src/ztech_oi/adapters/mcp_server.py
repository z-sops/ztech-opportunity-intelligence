"""MCP adapter (spec #28, #29). Thin: validate -> call ResearchService -> serialise.

No business logic lives here. No raw SQL, filesystem, shell, or credential
exposure — the only capabilities are the research operations below.

Transports: stdio (default, local dev / ZTech Electron child process).
`--transport streamable-http` is available for later remote deployment; the
engine is untouched by that choice.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..application.container import Engine, build_engine
from ..application.summary import SECTIONS, select_sections, summarize
from ..config import Settings
from ..domain.errors import EngineError
from ..domain.models import CompetitorHint, ProspectInput, ResearchOptions
from ..observability.logging import configure_logging

ResearchId = Annotated[str, Field(min_length=4, max_length=80, pattern=r"^[a-z]+_[A-Za-z0-9_]+$")]
ResponseMode = Literal["summary", "full"]

INSTRUCTIONS = (
    "ZTech Opportunity Intelligence. Use analyze_prospect_opportunity for a complete competitive analysis of a "
    "company. Every number in a report is backed by evidence_refs; opportunities and sales angles are inferences. "
    "Never present items listed in a sales angle's do_not_claim. Unavailable/unsupported providers mean 'not observed', "
    "never 'not active'. This server never sends outreach."
)


def create_server(engine_factory: Callable[[], Engine] | None = None) -> MCPServer:
    server = MCPServer(name="ztech-opportunity-intelligence", version="1.0.0", instructions=INSTRUCTIONS)
    holder: dict[str, Engine] = {}

    def engine() -> Engine:
        if "e" not in holder:
            holder["e"] = (engine_factory or (lambda: build_engine(Settings.from_env())))()
        return holder["e"]

    def fail(e: Exception) -> ToolError:
        if isinstance(e, EngineError):
            return ToolError(json.dumps(e.to_dict()))
        if isinstance(e, ValueError):
            return ToolError(json.dumps({"error": "VALIDATION_ERROR", "message": str(e)[:300]}))
        return ToolError(json.dumps({"error": "INTERNAL_ERROR", "message": type(e).__name__}))

    def shape(report, response: ResponseMode) -> dict[str, Any]:
        return summarize(report) if response == "summary" else report.model_dump(mode="json")

    @server.tool(
        description="Full pipeline: research the prospect, discover & research competitors, compare, and return "
        "opportunities, score, sales angles and timeline. response='summary' (default) or 'full'."
    )
    async def analyze_prospect_opportunity(
        prospect: ProspectInput, options: ResearchOptions | None = None, response: ResponseMode = "summary"
    ) -> dict[str, Any]:
        try:
            return shape(await engine().service.analyze_prospect(prospect, options), response)
        except Exception as e:
            raise fail(e) from e

    @server.tool(description="Research only the company itself (website, content, ads, social). No competitors.")
    async def research_company(prospect: ProspectInput, response: ResponseMode = "summary") -> dict[str, Any]:
        try:
            return shape(await engine().service.research_company(prospect), response)
        except Exception as e:
            raise fail(e) from e

    @server.tool(
        description="Identify likely competitors (with relationship_type, confidence, reason, evidence). Does not research them."
    )
    async def discover_competitors(
        prospect: ProspectInput, max_competitors: Annotated[int, Field(ge=1, le=10)] = 5
    ) -> dict[str, Any]:
        try:
            return await engine().service.discover_competitors(prospect, max_competitors)
        except Exception as e:
            raise fail(e) from e

    @server.tool(description="Research and compare the prospect against an explicit list of competitors (1-5).")
    async def research_competitors(
        prospect: ProspectInput,
        competitors: Annotated[list[CompetitorHint], Field(min_length=1, max_length=5)],
        response: ResponseMode = "summary",
    ) -> dict[str, Any]:
        try:
            report = await engine().service.research_competitors(prospect, [c.model_dump() for c in competitors])
            return shape(report, response)
        except Exception as e:
            raise fail(e) from e

    @server.tool(
        description="Re-run the deterministic analysis (comparisons, opportunities, score, angles) on a stored "
        "research with current evidence freshness. No network calls."
    )
    async def analyze_opportunity(research_id: ResearchId) -> dict[str, Any]:
        try:
            return engine().service.analyze_opportunity(research_id)
        except Exception as e:
            raise fail(e) from e

    @server.tool(
        description="Fetch the canonical IntelligenceReport (schema 1.0). Optionally limit to sections, e.g. "
        "['opportunities','sales_angles','evidence']."
    )
    async def get_opportunity_report(
        research_id: ResearchId, sections: Annotated[list[str] | None, Field(max_length=20)] = None
    ) -> dict[str, Any]:
        try:
            if sections and (unknown := set(sections) - SECTIONS):
                raise ValueError(f"unknown sections: {sorted(unknown)}; allowed: {sorted(SECTIONS)}")
            return select_sections(engine().service.get_report(research_id), sections)
        except Exception as e:
            raise fail(e) from e

    @server.tool(
        description="Merged opportunity timeline across all research runs for a prospect "
        "(by research_id, or by domain / company_name)."
    )
    async def get_opportunity_timeline(
        research_id: ResearchId | None = None,
        domain: Annotated[str | None, Field(max_length=253)] = None,
        company_name: Annotated[str | None, Field(max_length=120)] = None,
    ) -> dict[str, Any]:
        try:
            return engine().service.get_timeline(research_id=research_id, domain=domain, company_name=company_name)
        except Exception as e:
            raise fail(e) from e

    @server.tool(description="Engine version, schema version, which providers are configured, and how confidence is computed.")
    async def get_engine_info() -> dict[str, Any]:
        return engine().service.engine_info()

    return server


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ztech-oi-mcp", description="ZTech Opportunity Intelligence MCP server")
    ap.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    server = create_server(lambda: build_engine(settings))
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
