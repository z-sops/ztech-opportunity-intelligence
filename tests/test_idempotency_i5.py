"""I5: server-side idempotency. One caller intent = one key = at most one provider run.

A repeated key never reaches a provider again: RUNNING -> IN_PROGRESS (409, retryable),
FAILED -> the stored failure (terminal), finished -> the stored report, another prospect
-> IDEMPOTENCY_CONFLICT (409). Interrupted RUNNING jobs are closed at startup.
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
from ztech_oi.persistence.sqlite import SQLiteRepository

KEY = "ztech-L42-7f3a9c"
OTHER = {**PROSPECT, "company_name": "Rival Bakes", "domain": "rivalbakes.com"}


def running_job(key: str, prospect_key: str, *, age_s: int = 0, rid: str = "res_20260101000000_aaaaaaaa") -> ResearchJob:
    return ResearchJob(
        research_id=rid,
        prospect_entity_key=prospect_key,
        status=ResearchStatus.RUNNING,
        request={},
        idempotency_key=key,
        created_at=iso(utcnow() - timedelta(seconds=age_s)),
    )


def prospect_key(eng) -> str:
    from ztech_oi.domain.models import ProspectInput

    return eng.service._prospect(ProspectInput(**PROSPECT)).entity_key


async def test_finished_key_replays_the_stored_report_without_any_provider_call(market, search_market):
    eng = make_engine(market, search=search_market)
    a = await eng.service.analyze_prospect(PROSPECT, {"idempotency_key": KEY})
    calls, searches = len(market.calls), len(search_market.queries) if hasattr(search_market, "queries") else None
    b = await eng.service.analyze_prospect(PROSPECT, {"idempotency_key": KEY})
    assert b.research_id == a.research_id
    assert len(market.calls) == calls, "no web call on replay"
    if searches is not None:
        assert len(search_market.queries) == searches, "no search call on replay"
    await eng.aclose()


async def test_running_key_answers_in_progress_and_runs_nothing(market, search_market):
    repo = InMemoryRepository()
    eng = make_engine(market, search=search_market, repo=repo)
    repo.save_job(running_job(KEY, prospect_key(eng)))
    with pytest.raises(EngineError) as ei:
        await eng.service.analyze_prospect(PROSPECT, {"idempotency_key": KEY})
    assert ei.value.code is ErrorCode.IN_PROGRESS and ei.value.retryable is True
    assert ei.value.details["idempotent_replay"] is True
    assert not market.calls, "IN_PROGRESS must not reach any provider"
    await eng.aclose()


async def test_failed_key_replays_the_terminal_failure_and_runs_nothing(market, search_market):
    repo = InMemoryRepository()
    eng = make_engine(market, search=search_market, repo=repo)
    job = running_job(KEY, prospect_key(eng))
    job.status, job.completed_at = ResearchStatus.FAILED, iso(utcnow())
    job.error = {"error": "PROVIDER_FAILED", "message": "search provider failed", "retryable": False}
    repo.save_job(job)
    with pytest.raises(EngineError) as ei:
        await eng.service.analyze_prospect(PROSPECT, {"idempotency_key": KEY})
    assert ei.value.code is ErrorCode.PROVIDER_FAILED and ei.value.retryable is False
    assert ei.value.details == {"research_id": job.research_id, "idempotent_replay": True}
    assert not market.calls
    await eng.aclose()


async def test_key_reused_for_another_prospect_is_a_conflict(market, search_market):
    eng = make_engine(market, search=search_market)
    await eng.service.analyze_prospect(PROSPECT, {"idempotency_key": KEY})
    calls = len(market.calls)
    with pytest.raises(EngineError) as ei:
        await eng.service.analyze_prospect(OTHER, {"idempotency_key": KEY})
    assert ei.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert len(market.calls) == calls
    await eng.aclose()


async def test_new_key_is_a_new_snapshot_and_old_history_stays(market, search_market):
    eng = make_engine(market, search=search_market)
    a = await eng.service.analyze_prospect(PROSPECT, {"idempotency_key": KEY})
    b = await eng.service.analyze_prospect(PROSPECT, {"idempotency_key": KEY + "-2"})
    assert a.research_id != b.research_id and a.snapshot_id != b.snapshot_id
    assert eng.repo.get_report(a.research_id) is not None, "a refresh never overwrites an older report"
    await eng.aclose()


async def test_concurrent_same_key_runs_the_pipeline_once(market, search_market):
    eng = make_engine(market, search=search_market)
    results = await asyncio.gather(
        *(eng.service.analyze_prospect(PROSPECT, {"idempotency_key": KEY}) for _ in range(3)), return_exceptions=True
    )
    done = [r for r in results if not isinstance(r, BaseException)]
    busy = [r for r in results if isinstance(r, EngineError) and r.code is ErrorCode.IN_PROGRESS]
    assert len(done) + len(busy) == 3 and done
    assert len({r.research_id for r in done}) == 1
    assert sum(1 for j in eng.repo.jobs.values() if j.idempotency_key == KEY) == 1
    await eng.aclose()


@pytest.mark.parametrize("bad", ["", "has space", "x" * 129, "semi;colon"])
async def test_unsafe_keys_are_refused(market, bad):
    eng = make_engine(market)
    with pytest.raises(EngineError) as ei:
        await eng.service.analyze_prospect(PROSPECT, {"idempotency_key": bad})
    assert ei.value.code is ErrorCode.VALIDATION_ERROR and not market.calls
    await eng.aclose()


@pytest.mark.parametrize("make_repo", [InMemoryRepository, lambda: SQLiteRepository(":memory:")])
def test_claim_job_is_atomic_per_key(make_repo):
    repo = make_repo()
    assert repo.claim_job(running_job(KEY, "p", rid="res_20260101000000_00000001")) is True
    assert repo.claim_job(running_job(KEY, "p", rid="res_20260101000000_00000002")) is False, "same key"
    assert repo.claim_job(running_job("other-key", "p", rid="res_20260101000000_00000001")) is False, "same id"
    assert repo.get_job("res_20260101000000_00000002") is None
    repo.close()


@pytest.mark.parametrize("make_repo", [InMemoryRepository, lambda: SQLiteRepository(":memory:")])
def test_startup_closes_only_old_running_jobs(make_repo, market):
    repo = make_repo()
    repo.save_job(running_job("old-key", "p", age_s=3600, rid="res_20260101000000_0000000a"))
    repo.save_job(running_job("new-key", "p", age_s=60, rid="res_20260101000000_0000000b"))
    eng = make_engine(market, repo=repo)  # build_engine runs the recovery
    old, new = repo.get_job("res_20260101000000_0000000a"), repo.get_job("res_20260101000000_0000000b")
    assert old.status is ResearchStatus.FAILED and old.error["message"] == "interrupted" and old.completed_at
    assert new.status is ResearchStatus.RUNNING, "a job younger than the window may still be live"
    assert eng.service.recover_interrupted_jobs() == 0, "idempotent"


async def test_interrupted_key_gets_a_terminal_answer_not_in_progress_forever(market, search_market):
    repo = InMemoryRepository()
    probe = make_engine(market, repo=InMemoryRepository())
    repo.save_job(running_job(KEY, prospect_key(probe), age_s=3600))
    eng = make_engine(market, search=search_market, repo=repo)
    with pytest.raises(EngineError) as ei:
        await eng.service.analyze_prospect(PROSPECT, {"idempotency_key": KEY})
    assert ei.value.retryable is False and ei.value.details["idempotent_replay"] is True
    assert not market.calls
    await eng.aclose()


def test_rest_maps_in_progress_and_conflict_to_409(market, search_market):
    from fastapi.testclient import TestClient

    from ztech_oi.adapters.rest import create_app

    repo = InMemoryRepository()
    eng_holder = {}

    def factory():
        eng_holder["e"] = make_engine(market, search=search_market, repo=repo)
        return eng_holder["e"]

    app = create_app(factory, auth_token=None)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        repo.save_job(running_job(KEY, prospect_key(eng_holder["e"])))
        r = client.post("/v1/research", json={"prospect": PROSPECT, "options": {"idempotency_key": KEY}})
        assert r.status_code == 409 and r.json()["error"] == "IN_PROGRESS" and r.json()["retryable"] is True
        ok = client.post("/v1/research", json={"prospect": PROSPECT, "options": {"idempotency_key": "fresh-key-1"}})
        assert ok.status_code == 200
        c = client.post("/v1/research", json={"prospect": OTHER, "options": {"idempotency_key": "fresh-key-1"}})
        assert c.status_code == 409 and c.json()["error"] == "IDEMPOTENCY_CONFLICT"
