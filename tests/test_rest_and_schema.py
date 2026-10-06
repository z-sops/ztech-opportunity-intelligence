"""Optional REST adapter + canonical schema contract tests (spec #30, #43)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import PROSPECT, make_engine

from ztech_oi.domain.models import IntelligenceReport

ROOT = Path(__file__).resolve().parents[1]


def test_published_json_schema_is_current():
    published = json.loads((ROOT / "schemas" / "intelligence_report.v1.schema.json").read_text())
    assert published == IntelligenceReport.model_json_schema(), "run: python scripts/export_schema.py"


def test_example_output_validates_against_model():
    data = json.loads((ROOT / "examples" / "sample_output.json").read_text())
    IntelligenceReport.model_validate(data)


def test_rest_adapter(market, search_market):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ztech_oi.adapters.rest import create_app

    app = create_app(lambda: make_engine(market, search=search_market))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/v1/health").json()["status"] == "ok"
        r = client.post("/v1/research?response=summary", json={"prospect": PROSPECT})
        assert r.status_code == 200 and r.json()["opportunities"]
        rid = r.json()["research_id"]
        assert client.get(f"/v1/reports/{rid}?sections=opportunities").json()["research_id"] == rid
        assert client.get("/v1/reports/res_missing").status_code == 404
        bad = client.post("/v1/research", json={"prospect": {"company_name": "x", "domain": "localhost"}})
        assert bad.status_code == 400 and bad.json()["error"] == "INVALID_DOMAIN"
        assert client.get("/v1/timeline", params={"domain": "sweetcrumb.com"}).status_code == 200
