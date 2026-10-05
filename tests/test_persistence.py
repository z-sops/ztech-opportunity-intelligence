"""SQLite persistence, restart survival and repository contract (spec #31, #38 'restart persistence')."""

from __future__ import annotations

import pytest
from conftest import PROSPECT, make_engine
from fakes import build_site

from ztech_oi.domain.taxonomy import ChangeType
from ztech_oi.persistence.memory import InMemoryRepository
from ztech_oi.persistence.sqlite import SQLiteRepository


async def test_restart_persistence_and_cross_restart_diff(tmp_path, market, search_market):
    db = tmp_path / "oi.sqlite3"
    eng = make_engine(market, search=search_market, repo=SQLiteRepository(db))
    r1 = await eng.service.analyze_prospect(PROSPECT)
    counts = eng.repo.table_counts()
    await eng.aclose()
    assert all(counts[t] > 0 for t in ("research_jobs", "entities", "competitors", "evidence", "snapshots", "signals", "reports"))

    # "restart": brand-new engine + connection on the same file
    build_site(market, "cakehouse.com", name="Cake House", articles_days=[1, 2, 5, 12, 19, 33, 50], landing=5)
    eng2 = make_engine(market, search=search_market, repo=SQLiteRepository(db))
    loaded = eng2.service.get_report(r1.research_id)
    assert loaded.model_dump(exclude={"evidence", "limitations"}) == r1.model_dump(exclude={"evidence", "limitations"})
    r2 = await eng2.service.analyze_prospect(PROSPECT)
    assert r2.previous_snapshot_id == r1.snapshot_id
    assert any(c.type is ChangeType.NEW_PAGE and c.entity_name == "Cake House" for c in r2.changes)
    assert eng2.repo.table_counts()["evidence"] >= counts["evidence"]
    tl = eng2.service.get_timeline(domain="sweetcrumb.com")
    assert tl["research_ids"] == [r1.research_id, r2.research_id]
    await eng2.aclose()


@pytest.mark.parametrize("factory", [lambda p: SQLiteRepository(p / "x.db"), lambda p: InMemoryRepository()])
async def test_repository_contract(tmp_path, market, search_market, factory):
    repo = factory(tmp_path)
    eng = make_engine(market, search=search_market, repo=repo)
    r = await eng.service.analyze_prospect(PROSPECT, {"idempotency_key": "k1"})
    assert repo.get_job(r.research_id).status.value == r.status.value
    assert repo.find_job_by_idempotency_key("k1").research_id == r.research_id
    assert repo.get_entity(r.prospect.entity_id).company_name == "SweetCrumb Bakery"
    assert repo.get_snapshot(r.snapshot_id).research_id == r.research_id
    assert repo.latest_report(r.prospect.entity_key).research_id == r.research_id
    assert len(repo.get_evidence([e.evidence_id for e in r.evidence[:5]])) == 5
    snap = repo.get_snapshot(r.snapshot_id)
    with pytest.raises(Exception):
        repo.save_snapshot(snap)  # snapshots are immutable / append-only
    await eng.aclose()


def test_sqlite_schema_is_isolated(tmp_path):
    repo = SQLiteRepository(tmp_path / "y.db")
    tables = {r[0] for r in repo._q("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {
        "schema_meta",
        "research_jobs",
        "entities",
        "competitors",
        "evidence",
        "snapshots",
        "signals",
        "opportunities",
        "reports",
    }
    repo.close()
