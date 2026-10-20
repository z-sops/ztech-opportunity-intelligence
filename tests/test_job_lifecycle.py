"""G-44: the asynchronous research job lifecycle.

A full research takes roughly 20-90 s. Before this, the only route was the
synchronous `POST /v1/research`, so a caller with a short timeout could never
complete one and the work was lost when the caller gave up.

These tests pin the lifecycle:

    POST /v1/research/jobs              -> 202 {research_id, status: running}
    GET  /v1/research/jobs/{id}         -> the job state, its timestamps, its error
    GET  /v1/research/jobs/{id}/result  -> the report, or 409 while it runs

and the properties that make it safe to use:

  * a repeated idempotency key NEVER runs the pipeline twice;
  * a duplicate start while a run is in flight answers 409 IN_PROGRESS;
  * the same key for a different prospect answers 409 IDEMPOTENCY_CONFLICT;
  * a failed run reports its failure explicitly instead of hanging;
  * an interrupted run is closed at startup and reported as failed;
  * the synchronous route is unchanged.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import PROSPECT, make_engine

from ztech_oi.domain.errors import EngineError
from ztech_oi.domain.identity import iso, utcnow
from ztech_oi.domain.models import ResearchJob
from ztech_oi.domain.taxonomy import ErrorCode, ResearchStatus
from ztech_oi.persistence.memory import InMemoryRepository

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ztech_oi.adapters.rest import create_app  # noqa: E402

LOCAL = "http://127.0.0.1"
KEY = "ztech-L42-7f3a9c"
OTHER = {**PROSPECT, "company_name": "Rival Bakes", "domain": "rivalbakes.com"}


def _client(market, search_market, **kw):
    return TestClient(create_app(lambda: make_engine(market, search=search_market), **kw), base_url=LOCAL)


def _wait_for(client, research_id, tries=200):
    """Poll the status route until the job is no longer running."""
    for _ in range(tries):
        r = client.get(f"/v1/research/jobs/{research_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] != "running":
            return body
    raise AssertionError(f"job {research_id} never finished")


# --------------------------------------------------------------------- REST


def test_start_returns_202_and_the_job_can_be_polled_to_a_result(market, search_market):
    with _client(market, search_market) as c:
        started = c.post("/v1/research/jobs", json={"prospect": PROSPECT})
        assert started.status_code == 202, started.text
        body = started.json()
        rid = body["research_id"]
        assert body["status"] == "running"
        assert body["replayed"] is False
        assert body["report_available"] is False

        final = _wait_for(c, rid)
        assert final["status"] in ("completed", "partial")
        assert final["completed_at"]
        assert final["report_available"] is True

        result = c.get(f"/v1/research/jobs/{rid}/result")
        assert result.status_code == 200, result.text
        report = result.json()
        assert report["research_id"] == rid
        assert report["prospect"]["company_name"] == PROSPECT["company_name"]


def test_the_result_route_refuses_while_the_job_is_running(market, search_market):
    repo = InMemoryRepository()
    eng = make_engine(market, search=search_market, repo=repo)
    # A job that is running and will never finish on its own.
    repo.save_job(
        ResearchJob(
            research_id="res_20260101000000_aaaaaaaa",
            prospect_entity_key="dom:sweetcrumb.com",
            status=ResearchStatus.RUNNING,
            request={},
            created_at=iso(utcnow()),
        )
    )
    with TestClient(create_app(lambda: eng), base_url=LOCAL) as c:
        r = c.get("/v1/research/jobs/res_20260101000000_aaaaaaaa/result")
        assert r.status_code == 409, r.text
        assert r.json()["error"] == ErrorCode.IN_PROGRESS.value
        assert r.json()["retryable"] is True


def test_an_unknown_job_is_404(market, search_market):
    with _client(market, search_market) as c:
        assert c.get("/v1/research/jobs/res_20260101000000_aaaaaaaa").status_code == 404
        assert c.get("/v1/research/jobs/res_20260101000000_aaaaaaaa/result").status_code == 404


def test_a_malformed_research_id_is_422(market, search_market):
    with _client(market, search_market) as c:
        assert c.get("/v1/research/jobs/NOT-A-VALID-ID").status_code == 422
        assert c.get("/v1/research/jobs/NOT-A-VALID-ID/result").status_code == 422


# ------------------------------------------------------------- idempotency


def test_a_repeated_key_replays_the_finished_job_without_running_again(market, search_market):
    with _client(market, search_market) as c:
        first = c.post("/v1/research/jobs", json={"prospect": PROSPECT, "options": {"idempotency_key": KEY}})
        assert first.status_code == 202
        rid = first.json()["research_id"]
        _wait_for(c, rid)
        calls = len(market.calls)

        second = c.post("/v1/research/jobs", json={"prospect": PROSPECT, "options": {"idempotency_key": KEY}})
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["research_id"] == rid
        assert body["replayed"] is True
        assert body["report_available"] is True
        assert len(market.calls) == calls, "the replay reached no provider"


def test_the_same_key_for_a_different_prospect_is_a_conflict(market, search_market):
    with _client(market, search_market) as c:
        first = c.post("/v1/research/jobs", json={"prospect": PROSPECT, "options": {"idempotency_key": KEY}})
        rid = first.json()["research_id"]
        _wait_for(c, rid)
        again = c.post("/v1/research/jobs", json={"prospect": OTHER, "options": {"idempotency_key": KEY}})
        assert again.status_code == 409, again.text
        assert again.json()["error"] == ErrorCode.IDEMPOTENCY_CONFLICT.value


def test_a_duplicate_start_while_the_run_is_in_flight_is_in_progress(market, search_market):
    repo = InMemoryRepository()
    eng = make_engine(market, search=search_market, repo=repo)
    repo.save_job(
        ResearchJob(
            research_id="res_20260101000000_aaaaaaaa",
            prospect_entity_key="dom:sweetcrumb.com",
            status=ResearchStatus.RUNNING,
            request={},
            idempotency_key=KEY,
            created_at=iso(utcnow()),
        )
    )
    with TestClient(create_app(lambda: eng), base_url=LOCAL) as c:
        r = c.post("/v1/research/jobs", json={"prospect": PROSPECT, "options": {"idempotency_key": KEY}})
        assert r.status_code == 409, r.text
        assert r.json()["error"] == ErrorCode.IN_PROGRESS.value
        assert r.json()["retryable"] is True


# ------------------------------------------------------------- failure path


def test_a_failed_run_reports_its_failure_explicitly(market, search_market):
    repo = InMemoryRepository()
    eng = make_engine(market, search=search_market, repo=repo)
    repo.save_job(
        ResearchJob(
            research_id="res_20260101000000_aaaaaaaa",
            prospect_entity_key="dom:sweetcrumb.com",
            status=ResearchStatus.FAILED,
            request={},
            created_at=iso(utcnow()),
            completed_at=iso(utcnow()),
            error={"error": ErrorCode.PROVIDER_FAILED.value, "message": "the provider refused", "retryable": False},
        )
    )
    with TestClient(create_app(lambda: eng), base_url=LOCAL) as c:
        status = c.get("/v1/research/jobs/res_20260101000000_aaaaaaaa")
        assert status.status_code == 200
        body = status.json()
        assert body["status"] == "failed"
        assert body["error"]["message"] == "the provider refused"
        assert body["report_available"] is False

        result = c.get("/v1/research/jobs/res_20260101000000_aaaaaaaa/result")
        assert result.status_code == 409, result.text
        assert result.json()["error"] == ErrorCode.PROVIDER_FAILED.value
        assert "the provider refused" in result.json()["message"]


def test_an_interrupted_run_is_closed_at_startup_and_reported_as_failed(market, search_market):
    """A run whose process died must not wait forever."""
    repo = InMemoryRepository()
    eng = make_engine(market, search=search_market, repo=repo)
    repo.save_job(
        ResearchJob(
            research_id="res_20260101000000_aaaaaaaa",
            prospect_entity_key="dom:sweetcrumb.com",
            status=ResearchStatus.RUNNING,
            request={},
            created_at=iso(utcnow() - timedelta(hours=1)),
        )
    )
    closed = eng.service.recover_interrupted_jobs()
    assert closed == 1
    with TestClient(create_app(lambda: eng), base_url=LOCAL) as c:
        body = c.get("/v1/research/jobs/res_20260101000000_aaaaaaaa").json()
        assert body["status"] == "failed"
        assert body["error"]["message"] == "interrupted"


# --------------------------------------------------- the sync route is intact


def test_the_synchronous_route_still_works_and_still_reports_the_report(market, search_market):
    with _client(market, search_market) as c:
        r = c.post("/v1/research?response=summary", json={"prospect": PROSPECT})
        assert r.status_code == 200, r.text
        assert r.json()["status"] in ("completed", "partial")


# ----------------------------------------------------- the service contract


async def test_start_job_returns_a_descriptor_and_the_run_completes(market, search_market):
    eng = make_engine(market, search=search_market)
    started = await eng.service.start_job(PROSPECT, {"idempotency_key": KEY})
    assert started["status"] == "running"
    assert started["replayed"] is False
    rid = started["research_id"]

    for _ in range(200):
        state = eng.service.job_status(rid)
        if state["status"] != "running":
            break
        await asyncio.sleep(0.01)
    assert state["status"] in ("completed", "partial")
    assert state["report_available"] is True

    report = eng.service.job_result(rid)
    assert report.research_id == rid
    await eng.aclose()


async def test_job_result_raises_in_progress_while_the_job_runs(market, search_market):
    repo = InMemoryRepository()
    eng = make_engine(market, search=search_market, repo=repo)
    repo.save_job(
        ResearchJob(
            research_id="res_20260101000000_aaaaaaaa",
            prospect_entity_key="dom:sweetcrumb.com",
            status=ResearchStatus.RUNNING,
            request={},
            created_at=iso(utcnow()),
        )
    )
    with pytest.raises(EngineError) as ei:
        eng.service.job_result("res_20260101000000_aaaaaaaa")
    assert ei.value.code == ErrorCode.IN_PROGRESS
    assert ei.value.retryable is True
    assert ei.value.details["research_id"] == "res_20260101000000_aaaaaaaa"
    await eng.aclose()


async def test_a_replayed_key_does_not_start_a_second_run(market, search_market):
    eng = make_engine(market, search=search_market)
    first = await eng.service.start_job(PROSPECT, {"idempotency_key": KEY})
    for _ in range(200):
        if eng.service.job_status(first["research_id"])["status"] != "running":
            break
        await asyncio.sleep(0.01)
    calls = len(market.calls)

    second = await eng.service.start_job(PROSPECT, {"idempotency_key": KEY})
    assert second["research_id"] == first["research_id"]
    assert second["replayed"] is True
    assert second["report_available"] is True
    assert len(market.calls) == calls, "the replay reached no provider"
    await eng.aclose()
