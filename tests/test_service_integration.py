"""End-to-end research through the application service (spec #38 integration scenarios)."""

from __future__ import annotations

import json

import pytest
from conftest import PROSPECT, fast_settings, make_engine
from fakes import FakeSearch, build_site, meta_payload, sr

from ztech_oi.domain.errors import EngineError, NotFound
from ztech_oi.domain.models import IntelligenceReport
from ztech_oi.domain.taxonomy import (
    ChangeType,
    ClaimKind,
    ErrorCode,
    OpportunityType,
    ProviderStatus,
    ResearchStatus,
    TimelineEventType,
)
from ztech_oi.persistence.memory import InMemoryRepository


async def test_full_research_happy_path(market, search_market):
    eng = make_engine(market, search=search_market)
    r = await eng.service.analyze_prospect(PROSPECT)
    IntelligenceReport.model_validate_json(r.model_dump_json())  # canonical, JSON round-trip
    assert r.schema_version == "1.0" and r.status is ResearchStatus.COMPLETED
    assert [c.domain for c in r.competitors] == ["rivalbakes.com", "cakehouse.com"]
    assert all(c.evidence_refs and c.reason for c in r.competitors)
    types = {o.type for o in r.opportunities}
    assert OpportunityType.CONTENT_GAP in types and OpportunityType.LANDING_PAGE_GAP in types
    # every opportunity / angle / comparison number is backed by evidence that exists in the report
    ids = {e.evidence_id for e in r.evidence}
    for o in r.opportunities:
        assert o.claim_kind is ClaimKind.INFERENCE and o.evidence_refs and set(o.evidence_refs) <= ids
    for a in r.sales_angles:
        assert set(a.evidence_refs) <= ids and a.do_not_claim and a.confidence <= 1
    for c in r.comparisons:
        assert set(c.evidence_refs) <= ids
    # honest provider statuses
    st = r.provider_status[r.prospect.entity_id]
    assert st["linkedin"] is ProviderStatus.UNSUPPORTED and st["meta_ads"] is ProviderStatus.UNAVAILABLE
    assert any("Meta provider: NOT CONFIGURED" in x for x in r.limitations)
    # timeline only contains engine-owned events
    assert {e.event_type for e in r.timeline} >= {
        TimelineEventType.RESEARCH_STARTED,
        TimelineEventType.SNAPSHOT_CAPTURED,
        TimelineEventType.ARTICLE_PUBLISHED,
        TimelineEventType.OPPORTUNITY_IDENTIFIED,
    }
    assert not any(e.event_type.value in ("EMAIL_SENT", "PITCH_APPROVED") for e in r.timeline)
    assert r.prospect.profile.emails and r.prospect.profile.field_evidence
    assert 0 <= r.opportunity_score.score <= 100
    await eng.aclose()


async def test_repeated_research_detects_changes_and_dedups_evidence(market, search_market):
    repo = InMemoryRepository()
    eng = make_engine(market, search=search_market, repo=repo)
    r1 = await eng.service.analyze_prospect(PROSPECT)
    n_ev = repo.count_evidence()
    r_same = await eng.service.analyze_prospect(PROSPECT)
    assert repo.count_evidence() == n_ev, "same-day re-run must not duplicate evidence"
    assert r_same.previous_snapshot_id == r1.snapshot_id
    assert [c for c in r_same.changes if c.type is not ChangeType.PROVIDER_AVAILABILITY_CHANGED] == []
    # competitor launches new landing pages + articles -> changes, signals, momentum
    build_site(
        market,
        "rivalbakes.com",
        name="Rival Bakes",
        articles_days=[1, 2, 3, 8, 15, 22, 30, 41, 55],
        landing=7,
        careers=True,
        offers="20% off wedding tastings this month",
        extra_pages=["/locations/evanston"],
    )
    r2 = await eng.service.analyze_prospect(PROSPECT)
    kinds = {c.type for c in r2.changes}
    assert {ChangeType.NEW_PAGE, ChangeType.NEW_ARTICLE} <= kinds
    sig = {s.type.value for s in r2.signals}
    assert {"NEW_LANDING_PAGE", "EXPANSION", "NEW_ARTICLE"} <= sig
    assert r2.opportunity_score.previous_score == r_same.opportunity_score.score
    comps = {c.key: c for c in r2.opportunity_score.components}
    assert comps["recent_changes"].computed and comps["market_timing"].computed
    tl = eng.service.get_timeline(research_id=r2.research_id)
    assert len(tl["research_ids"]) == 3 and len(tl["score_history"]) == 3
    ids = [e["event_id"] for e in tl["events"]]
    assert len(ids) == len(set(ids))
    await eng.aclose()


async def test_idempotency_key_returns_same_report(market, search_market):
    eng = make_engine(market, search=search_market)
    a = await eng.service.analyze_prospect(PROSPECT, {"idempotency_key": "ztech-lead-42"})
    calls = len(market.calls)
    b = await eng.service.analyze_prospect(PROSPECT, {"idempotency_key": "ztech-lead-42"})
    assert a.research_id == b.research_id and len(market.calls) == calls
    await eng.aclose()


async def test_known_competitors_without_search(market):
    from ztech_oi.integrations.search import NullSearch

    eng = make_engine(market, search=NullSearch())
    payload = {
        **PROSPECT,
        "known_competitors": [
            {"company_name": "Rival Bakes", "domain": "rivalbakes.com"},
            {"company_name": "Rival again", "domain": "www.rivalbakes.com"},  # duplicate
            {"company_name": "SweetCrumb", "domain": "sweetcrumb.com"},  # the prospect itself
        ],
    }
    r = await eng.service.analyze_prospect(payload)
    assert [c.domain for c in r.competitors] == ["rivalbakes.com"]
    assert r.competitors[0].relationship_type.value == "user_provided"
    await eng.aclose()


async def test_partial_research_when_competitor_site_down(market, search_market):
    market.add("https://cakehouse.com/", "down", status=500)
    eng = make_engine(market, search=search_market)
    r = await eng.service.analyze_prospect(PROSPECT)
    assert r.status is ResearchStatus.PARTIAL
    cake = next(c for c in r.competitors if c.domain == "cakehouse.com")
    assert r.provider_status[cake.entity_id]["website"] is ProviderStatus.UNAVAILABLE
    for c in r.comparisons:
        if c.competitor_id == cake.entity_id:
            assert c.interpretation.value == "insufficient_evidence"
    assert any(e.event_type is TimelineEventType.RESEARCH_PARTIAL for e in r.timeline)
    await eng.aclose()


async def test_company_not_found_is_failed_not_fake(web):
    eng = make_engine(web, search=FakeSearch())
    r = await eng.service.analyze_prospect({"company_name": "Nobody Co", "domain": "nobody-xyz.com"})
    assert r.status is ResearchStatus.FAILED and r.opportunities == [] and r.sales_angles == []
    assert r.limitations[0].startswith("COMPANY_NOT_FOUND")
    await eng.aclose()


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"company_name": ""}, ErrorCode.VALIDATION_ERROR),
        ({}, ErrorCode.VALIDATION_ERROR),
        ({"company_name": "A", "domain": "localhost"}, ErrorCode.INVALID_DOMAIN),
        ({"company_name": "A", "domain": "http://10.0.0.1"}, ErrorCode.INVALID_DOMAIN),
        ({"company_name": "A", "unexpected": 1}, ErrorCode.VALIDATION_ERROR),
        ({"company_name": "A" * 500}, ErrorCode.VALIDATION_ERROR),
    ],
)
async def test_invalid_inputs_are_rejected(web, payload, code):
    eng = make_engine(web)
    with pytest.raises(EngineError) as ei:
        await eng.service.analyze_prospect(payload)
    assert ei.value.code is code
    await eng.aclose()


async def test_unknown_provider_option_rejected(web):
    eng = make_engine(web)
    with pytest.raises(EngineError):
        await eng.service.analyze_prospect(PROSPECT, {"providers": ["rm -rf"]})


async def test_research_company_only_and_provider_allowlist(market):
    eng = make_engine(market)
    r = await eng.service.research_company(PROSPECT)
    assert r.competitors == [] and r.comparisons == []
    r2 = await eng.service.analyze_prospect(PROSPECT, {"providers": ["website"], "max_competitors": 0})
    assert set(r2.provider_status[r2.prospect.entity_id]) == {"website"}
    await eng.aclose()


async def test_meta_configured_end_to_end_with_reliable_gap(market, search_market):
    def route(request):
        from fakes import Route, query

        term = query(request)["search_terms"][0]
        n = {"Rival Bakes": 9, "Cake House": 6}.get(term, 0)
        return Route(200, json.dumps(meta_payload(term, n)).encode(), {"content-type": "application/json"})

    market.add_fn("https://graph.facebook.com/v21.0/ads_archive", route)
    s = fast_settings(meta_token="SECRET_META_TOKEN")
    s.meta_ad_countries = ["DE"]
    eng = make_engine(market, search=search_market, settings=s)
    r = await eng.service.analyze_prospect(PROSPECT)
    types = {o.type for o in r.opportunities}
    assert {OpportunityType.ADVERTISING_GAP, OpportunityType.COMPETITIVE_VISIBILITY_GAP} <= types
    assert "SECRET_META_TOKEN" not in r.model_dump_json()
    assert any(e.event_type is TimelineEventType.META_AD_STARTED for e in r.timeline)
    angle = next(a for a in r.sales_angles if "advertising" in a.angle.lower())
    assert any("spend" in d.lower() for d in angle.do_not_claim)
    await eng.aclose()


async def test_analyze_opportunity_is_offline_and_get_report_not_found(market, search_market):
    eng = make_engine(market, search=search_market)
    r = await eng.service.analyze_prospect(PROSPECT)
    calls = len(market.calls)
    out = eng.service.analyze_opportunity(r.research_id)
    assert len(market.calls) == calls
    assert out["opportunity_score"]["score"] == r.opportunity_score.score
    with pytest.raises(NotFound):
        eng.service.get_report("res_missing")
    await eng.aclose()


async def test_discover_competitors_service(market, search_market):
    eng = make_engine(market, search=search_market)
    out = await eng.service.discover_competitors(PROSPECT, 1)
    assert out["status"] == "success" and len(out["competitors"]) == 1
    await eng.aclose()


async def test_concurrent_research_does_not_mix_data(market, search_market):
    import asyncio

    build_site(market, "otherco.com", name="Other Co", articles_days=[1, 2, 3])
    eng = make_engine(market, search=FakeSearch(default=[sr("https://rivalbakes.com/", "Rival Bakes")]))
    a, b = await asyncio.gather(
        eng.service.analyze_prospect(PROSPECT),
        eng.service.analyze_prospect({"company_name": "Other Co", "domain": "otherco.com"}),
    )
    assert {e.entity_id for e in a.evidence} <= {a.prospect.entity_id, *[c.entity_id for c in a.competitors]}
    assert {e.entity_id for e in b.evidence} <= {b.prospect.entity_id, *[c.entity_id for c in b.competitors]}
    assert a.prospect.entity_id != b.prospect.entity_id
    await eng.aclose()
