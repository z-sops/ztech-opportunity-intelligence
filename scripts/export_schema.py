"""Regenerate schemas/*.json and examples/sample_output.json (deterministic offline fixture run)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from ztech_oi.domain.models import IntelligenceReport, ProspectInput, ResearchOptions  # noqa: E402


def export_schemas() -> None:
    out = ROOT / "schemas"
    out.mkdir(exist_ok=True)
    (out / "intelligence_report.v1.schema.json").write_text(json.dumps(IntelligenceReport.model_json_schema(), indent=2))
    (out / "prospect_input.v1.schema.json").write_text(json.dumps(ProspectInput.model_json_schema(), indent=2))
    (out / "research_options.v1.schema.json").write_text(json.dumps(ResearchOptions.model_json_schema(), indent=2))


async def export_examples() -> None:
    from conftest import PROSPECT, make_engine  # test fixtures double as a reproducible demo
    from fakes import FakeSearch, FakeWeb, build_site, sr

    web = FakeWeb()
    build_site(web, "sweetcrumb.com", name="SweetCrumb Bakery", articles_days=[45, 200])
    build_site(
        web,
        "rivalbakes.com",
        name="Rival Bakes",
        articles_days=[2, 8, 15, 22, 30, 41, 55],
        landing=4,
        careers=True,
        offers="20% off wedding tastings this month",
    )
    build_site(web, "cakehouse.com", name="Cake House", articles_days=[5, 12, 19, 33, 50], landing=3)
    search = FakeSearch(
        default=[
            sr("https://rivalbakes.com/", "Rival Bakes | Wedding Cakes Chicago"),
            sr("https://cakehouse.com/", "Cake House - Custom cakes"),
        ]
    )
    eng = make_engine(web, search=search)
    report = await eng.service.analyze_prospect(PROSPECT)
    ex = ROOT / "examples"
    ex.mkdir(exist_ok=True)
    (ex / "sample_request.json").write_text(json.dumps({"prospect": PROSPECT, "options": {"max_competitors": 3}}, indent=2))
    (ex / "sample_output.json").write_text(json.dumps(report.model_dump(mode="json"), indent=2))
    from ztech_oi.application.summary import summarize

    (ex / "sample_output_summary.json").write_text(json.dumps(summarize(report), indent=2))
    await eng.aclose()


if __name__ == "__main__":
    export_schemas()
    asyncio.run(export_examples())
    print("schemas/ and examples/ regenerated")
