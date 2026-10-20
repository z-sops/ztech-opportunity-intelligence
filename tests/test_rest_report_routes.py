"""REST-level contract tests for the report routes.

Audit gap G-49: the REST routes accepted any ``research_id`` string, while the
MCP adapter already enforced ``^[a-z]+_[A-Za-z0-9_]+$``. A malformed id
reached the database layer instead of being rejected at the edge.

These tests pin the REST behaviour to the same contract the MCP adapter uses,
and assert that a malformed id is a 422 (not a 500 and not a database error).
"""

from conftest import make_engine
from fastapi.testclient import TestClient

from ztech_oi.adapters.rest import create_app

LOCAL = "http://127.0.0.1"


def _client(market, search_market):
    return TestClient(
        create_app(lambda: make_engine(market, search=search_market), auth_token=None),
        base_url=LOCAL,
    )


def test_get_report_rejects_a_malformed_research_id(market, search_market):
    with _client(market, search_market) as c:
        response = c.get("/v1/reports/not-a-valid-id")
        assert response.status_code == 422, response.text


def test_get_report_rejects_a_path_traversal_attempt(market, search_market):
    with _client(market, search_market) as c:
        response = c.get("/v1/reports/..%2F..%2Fetc%2Fpasswd")
        assert response.status_code in (404, 422), response.text


def test_analyze_rejects_a_malformed_research_id(market, search_market):
    with _client(market, search_market) as c:
        response = c.post("/v1/reports/NOT-A-VALID-ID/analyze")
        assert response.status_code == 422, response.text


def test_a_well_formed_but_unknown_research_id_is_a_domain_error_not_a_crash(market, search_market):
    # The id passes the shape check, so the request reaches the service, which
    # reports NOT_FOUND. It must never be a 500.
    with _client(market, search_market) as c:
        response = c.get("/v1/reports/res_20240101000000_abcdef01")
        assert response.status_code == 404, response.text
        assert response.json()["error"] == "NOT_FOUND"


def test_the_health_route_stays_unauthenticated(market, search_market):
    with _client(market, search_market) as c:
        response = c.get("/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
