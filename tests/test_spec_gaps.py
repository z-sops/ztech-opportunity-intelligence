"""Tests for the second-pass spec coverage: topic-scoped comparisons (§4/§16), competitor location (§10),
PARTIAL_RESEARCH (§35), provider telemetry logs (§37), CTA/service/topic-tagged page diffs (§15),
Google & X diffs (§19), Meta/Google/X depth (§11/§12/§14), business model (§9), service split (§40),
signal_refs on change-driven opportunities (§21)."""

from __future__ import annotations

import json
import logging

from conftest import PROSPECT, fast_settings, make_engine
from fakes import FakeWeb, Route, build_site, days_ago, html_page, meta_payload, query
from test_engine import OK, inv, obs, snap
from test_providers import http, req

from ztech_oi.application.competitor_service import CompetitorService
from ztech_oi.application.opportunity_service import OpportunityService
from ztech_oi.domain.models import Entity
from ztech_oi.domain.taxonomy import (
    ChangeType,
    ComparisonDimension,
    ComparisonInterpretation,
    EntityKind,
    ErrorCode,
    ObservationType,
    OpportunityType,
    ProviderStatus,
    ResearchStatus,
    SignalType,
)
from ztech_oi.engine.diff import ChangeDetector
from ztech_oi.net.parsing import parse_html, topic_matches
from ztech_oi.persistence.memory import InMemoryRepository
from ztech_oi.providers.ads import GoogleAdsProvider, MetaAdsProvider
from ztech_oi.providers.social import TwitterProvider, classify_post
from ztech_oi.providers.website import WebsiteProvider, infer_business_model


def _cmp(report, dim):
    return [c for c in report.comparisons if c.dimension is dim]


# ------------------------------------------------------------------ §4 / §16
def test_topic_matching_is_phrase_based():
    assert topic_matches("Order your wedding cake today", ["wedding cakes"]) == ["wedding cakes"]
    assert topic_matches("Birthday cakes for kids", ["wedding cakes"]) == []
    assert topic_matches("Corporate event catering", ["corporate catering"]) == ["corporate catering"]


async def test_topic_scoped_content_comparison_and_topical_opportunity(market, search_market):
    eng = make_engine(market, search=search_market)
    r = await eng.service.analyze_prospect(PROSPECT)
    topical = _cmp(r, ComparisonDimension.RELEVANT_CONTENT_60D)
    assert topical and all(c.topic == "wedding cakes" for c in topical)
    rival = next(c for c in topical if c.competitor_observed == 7)
    assert rival.prospect_observed == 1 and rival.interpretation is ComparisonInterpretation.COMPETITOR_MORE_ACTIVE
    gap = next(o for o in r.opportunities if o.type is OpportunityType.CONTENT_GAP)
    assert gap.title.endswith("around wedding cakes")
    assert "topic_articles about wedding cakes" in gap.what_was_observed
    await eng.aclose()


async def test_topic_dimensions_skipped_without_products_services(market, search_market):
    eng = make_engine(market, search=search_market)
    r = await eng.service.analyze_prospect({k: v for k, v in PROSPECT.items() if k != "products_services"})
    assert not _cmp(r, ComparisonDimension.RELEVANT_CONTENT_60D)
    assert not _cmp(r, ComparisonDimension.RELEVANT_AD_CREATIVES)
    await eng.aclose()


def _meta_route(counts: dict[str, int], video_ids: set[str] | None = None):
    def route(request):
        q = query(request)
        term = q["search_terms"][0]
        payload = meta_payload(term, counts.get(term, 0), prefix=term[:3])
        if q.get("media_type", [""])[0] == "VIDEO":
            payload = {"data": [{"id": a["id"]} for a in payload["data"] if a["id"] in (video_ids or set())]}
        return Route(200, json.dumps(payload).encode(), {"content-type": "application/json"})

    return route


async def test_meta_depth_offers_ctas_landing_video_and_topic(web):
    web.add_fn("https://graph.facebook.com/v21.0/ads_archive", _meta_route({"SweetCrumb Bakery": 4}, {"Swe0", "Swe1"}))
    r = req()
    res = await MetaAdsProvider(http(web), "T", ["DE"]).collect(r)
    o = next(x for x in res.observations if x.type is ObservationType.ADS_META)
    m = o.metrics
    assert m["active_creative_count"] == 4 and m["offer_creative_count"] == 4  # fixture copy says "offer"
    assert m["relevant_creative_count"] == 4  # "wedding cake" matches products_services "wedding cakes"
    assert m["video_creative_count"] == 2 and m["non_video_creative_count"] == 2
    attrs = o.items[-1]
    assert attrs["ctas"][0] == {"cta": "Book a tasting", "creatives": 4}
    assert attrs["landing_domains"][0]["domain"] == "example.com"
    assert {i.get("media") for i in o.items if i.get("ad_id")} == {"video", "non_video"}


async def test_relevant_ad_creatives_comparison_end_to_end(market, search_market):
    market.add_fn("https://graph.facebook.com/v21.0/ads_archive", _meta_route({"Rival Bakes": 8, "Cake House": 6}))
    s = fast_settings(meta_token="T")
    s.meta_ad_countries = ["DE"]
    eng = make_engine(market, search=search_market, settings=s)
    r = await eng.service.analyze_prospect(PROSPECT)
    rel = _cmp(r, ComparisonDimension.RELEVANT_AD_CREATIVES)
    assert {c.competitor_observed for c in rel} == {8, 6} and all(c.prospect_observed == 0 for c in rel)
    ad = next(o for o in r.opportunities if o.type is OpportunityType.ADVERTISING_GAP)
    assert ad.title.endswith("around wedding cakes") and "about wedding cakes" in ad.what_was_observed
    await eng.aclose()


# ----------------------------------------------------------------- §11 Google
async def test_google_copy_offers_topics(web):
    web.add(
        "https://serpapi.com/search.json",
        {
            "ad_creatives": [
                {
                    "advertiser": "SweetCrumb Bakery",
                    "ad_creative_id": "G1",
                    "format": "text",
                    "target_domain": "sweetcrumb.com",
                    "title": "Wedding Cakes Chicago",
                    "snippet": "20% off tastings this month",
                    "link": "https://sweetcrumb.com/offers/w",
                },
                {"advertiser": "SweetCrumb Bakery", "ad_creative_id": "G2", "format": "image", "target_domain": "sweetcrumb.com"},
            ]
        },
    )
    res = await GoogleAdsProvider(http(web), "K").collect(req())
    m = res.observations[0].metrics
    assert m["creative_count"] == 2 and m["creatives_with_copy"] == 1
    assert m["offer_creative_count"] == 1 and m["relevant_creative_count"] == 1
    assert res.observations[0].items[0]["landing_domain"] == "sweetcrumb.com"


async def test_google_without_copy_reports_unknown_not_zero(web):
    web.add(
        "https://serpapi.com/search.json",
        {
            "ad_creatives": [
                {"advertiser": "SweetCrumb Bakery", "ad_creative_id": "G1", "format": "image", "target_domain": "sweetcrumb.com"}
            ]
        },
    )
    res = await GoogleAdsProvider(http(web), "K").collect(req())
    m = res.observations[0].metrics
    assert m["offer_creative_count"] is None and m["relevant_creative_count"] is None
    assert any("no ad copy" in x for x in res.limitations)


# ---------------------------------------------------------------------- §14 X
def test_post_classification_rules():
    assert classify_post("Introducing our new wedding menu!", False) == ["product_launch"]
    assert "offer" in classify_post("20% off all cakes this weekend", False)
    assert classify_post("@jane thanks for the order!", True) == ["customer_conversation"]
    assert classify_post("Lovely morning in Chicago", False) == ["general"]


async def test_x_metrics_replies_labels_and_topics(web):
    web.add(
        "https://api.x.com/2/users/by/username/sweetcrumb", {"data": {"id": "42", "public_metrics": {"followers_count": 900}}}
    )
    web.add(
        "https://api.x.com/2/users/42/tweets",
        {
            "data": [
                {
                    "id": "1",
                    "text": "Introducing our new wedding cake collection",
                    "created_at": days_ago(2).isoformat(),
                    "public_metrics": {"like_count": 10},
                },
                {
                    "id": "2",
                    "text": "We're excited to announce longer hours",
                    "created_at": days_ago(5).isoformat(),
                    "public_metrics": {"like_count": 4},
                },
                {"id": "3", "text": "@sam happy to help", "created_at": days_ago(1).isoformat(), "in_reply_to_user_id": "7"},
            ]
        },
    )
    res = await TwitterProvider(http(web), "B").collect(req(social_profiles={"x": "https://x.com/sweetcrumb"}))
    m = res.observations[0].metrics
    assert (m["posts_last_30d"], m["replies_last_30d"]) == (2, 1)
    assert m["product_launch_posts_last_30d"] == 1 and m["announcements_last_30d"] == 1
    assert m["relevant_posts_last_30d"] == 1 and m["avg_likes_per_post"] == 7.0
    assert any(e.claim_kind.value == "inference" and "Rule-based labels" in e.claim for e in res.evidence)


# ------------------------------------------------------------------ §9 model
def test_business_model_needs_two_indicators():
    saas = parse_html("<a href='/signup'>Sign up</a><p>Start your free trial. $29 per month.</p>", "https://s.com/")
    assert set(infer_business_model([saas], ["Stripe"])) == {"saas_subscription"}
    weak = parse_html("<p>Visit us downtown</p>", "https://w.com/")
    assert infer_business_model([weak], []) == {}


async def test_business_model_in_profile_with_provenance(web):
    build_site(web, "sweetcrumb.com", name="S", articles_days=[])
    res = await WebsiteProvider(http(web)).collect(req())
    prof = next(o for o in res.observations if o.type is ObservationType.WEBSITE_COMPANY_PROFILE).items[0]
    assert "food_service" in prof["business_model"]
    ev = [e for e in res.evidence if e.evidence_id in prof["field_evidence"]["business_model"]]
    assert ev and all(e.claim_kind.value == "inference" for e in ev)


# --------------------------------------------------- §10 location, §35, §37
async def test_competitor_location_from_own_site_and_partial_code(market, search_market, caplog):
    market.add("https://cakehouse.com/", "down", status=500)
    eng = make_engine(market, search=search_market)
    with caplog.at_level(logging.INFO, logger="ztech_oi.providers"):
        r = await eng.service.analyze_prospect(PROSPECT)
    rival = next(c for c in r.competitors if c.domain == "rivalbakes.com")
    cake = next(c for c in r.competitors if c.domain == "cakehouse.com")
    assert rival.location == "Chicago, IL"  # from the competitor's own JSON-LD
    assert cake.location is None  # site down -> never assumed
    assert r.status is ResearchStatus.PARTIAL and r.limitations[0].startswith(ErrorCode.PARTIAL_RESEARCH.value)
    assert "website@Cake House=unavailable" in r.limitations[0]
    lines = [rec for rec in caplog.records if rec.getMessage() == "provider_finished"]
    assert len(lines) == len(r.telemetry.providers)
    t = lines[0].telemetry
    assert {"research_id", "provider", "status", "duration_ms", "evidence_created", "retry_count"} <= set(t)
    await eng.aclose()


# --------------------------------------------------------------- §15 / §19 diffs
def _profile_obs(ent, ctas, services):
    return obs(ent, ObservationType.WEBSITE_COMPANY_PROFILE, {}, ["e"], items=[{"ctas": ctas, "services": services}])


def test_diff_cta_and_service_changes():
    a = snap("s1", [_profile_obs("ent_c", ["Order now"], ["Wedding cakes"])], OK)
    b = snap("s2", [_profile_obs("ent_c", ["Order now", "Book a tasting"], ["Wedding cakes", "Corporate catering"])], OK)
    ch = {c.type: c for c in ChangeDetector().diff(a, b)}
    assert "Book a tasting" in ch[ChangeType.CTA_CHANGED].detail
    assert "Corporate catering" in ch[ChangeType.SERVICES_CHANGED].detail


def test_diff_new_urls_are_topic_tagged():
    base = [("https://c.com/", "homepage")]
    a = snap("s1", [inv("ent_c", base)], OK)
    b = snap(
        "s2",
        [
            inv(
                "ent_c",
                base
                + [
                    ("https://c.com/wedding-cakes/summer", "other"),
                    ("https://c.com/blog/wedding-cake-trends", "blog"),
                    ("https://c.com/about-team", "about"),
                ],
            )
        ],
        OK,
    )
    changes = ChangeDetector(topics=["wedding cakes"]).diff(a, b)
    summary = next(c for c in changes if c.type is ChangeType.PAGE_INVENTORY_CHANGED)
    assert "1 -> 4 pages; 3 new URL(s)" in summary.detail and "2 appear related to wedding cakes" in summary.detail
    tagged = [c for c in changes if c.type is ChangeType.NEW_PAGE and c.after["topics"]]
    assert len(tagged) == 2


def test_diff_google_and_x_activity():
    def g(ids, recent):
        return obs(
            "ent_c", ObservationType.ADS_GOOGLE, {"shown_last_30d": recent}, ["e"], items=[{"creative_id": i} for i in ids]
        )

    def x(n):
        return obs("ent_c", ObservationType.SOCIAL_TWITTER, {"posts_last_30d": n}, ["e"])

    st = {"ent_c": {"google_ads": ProviderStatus.SUCCESS, "twitter": ProviderStatus.SUCCESS}}
    a = snap("s1", [g(["a", "b"], 2), x(3)], st)
    b = snap("s2", [g(["a", "c", "d", "e"], 5), x(10)], st)
    changes = ChangeDetector().diff(a, b)
    kinds = {(c.type, c.channel) for c in changes}
    assert (ChangeType.NEW_AD_CREATIVE, "google_ads") in kinds and (ChangeType.REMOVED_AD_CREATIVE, "google_ads") in kinds
    assert (ChangeType.AD_ACTIVITY_INCREASED, "google_ads") in kinds
    assert (ChangeType.SOCIAL_ACTIVITY_INCREASED, "twitter") in kinds
    # X unavailable in the second run -> availability change only, never a decrease
    b2 = snap("s3", [], {"ent_c": {"google_ads": ProviderStatus.SUCCESS, "twitter": ProviderStatus.UNAVAILABLE}})
    assert not any(c.type is ChangeType.SOCIAL_ACTIVITY_DECREASED for c in ChangeDetector().diff(a, b2))


async def test_repeat_run_emits_topic_page_summary_and_change_signal_refs(market, search_market):
    eng = make_engine(market, search=search_market, repo=InMemoryRepository())
    await eng.service.analyze_prospect(PROSPECT)
    build_site(
        market,
        "rivalbakes.com",
        name="Rival Bakes",
        articles_days=[1, 2, 3, 8, 15, 22, 30, 41, 55],
        landing=4,
        careers=True,
        offers="20% off wedding tastings this month",
        extra_pages=["/wedding-cakes/summer-collection", "/wedding-cakes/tasting-day"],
    )
    market.add(
        "https://rivalbakes.com/services",
        html_page("Services", "<h1>Services</h1><h2>Wedding cakes</h2><h2>Corporate catering</h2><h2>Dessert tables</h2>"),
    )
    r2 = await eng.service.analyze_prospect(PROSPECT)
    summary = next(c for c in r2.changes if c.type is ChangeType.PAGE_INVENTORY_CHANGED and c.entity_name == "Rival Bakes")
    assert "appear related to wedding cakes" in summary.detail
    assert any(c.type is ChangeType.SERVICES_CHANGED for c in r2.changes)
    assert any(s.type is SignalType.MESSAGING_CHANGE for s in r2.signals)
    momentum = next(o for o in r2.opportunities if o.type is OpportunityType.COMPETITOR_MOMENTUM)
    assert momentum.change_refs and momentum.signal_refs, "change-driven opportunities must cite their signals"
    await eng.aclose()


# ------------------------------------------------------------------- §40
def test_service_split_exists_and_is_used(web):
    eng = make_engine(web)
    assert isinstance(eng.service.competitors, CompetitorService)
    assert isinstance(eng.service.analysis, OpportunityService)


async def test_hints_only_discovery_makes_no_search_calls(market):
    from fakes import FakeSearch

    search = FakeSearch()
    eng = make_engine(market, search=search)
    payload = {**PROSPECT, "known_competitors": [{"company_name": "Rival Bakes", "domain": "rivalbakes.com"}]}
    r = await eng.service.analyze_prospect(payload, {"discover_competitors": False})
    assert search.queries == [] and [c.domain for c in r.competitors] == ["rivalbakes.com"]
    await eng.aclose()


def test_entity_kind_import_sanity():
    assert Entity(entity_id="e", entity_key="k", kind=EntityKind.PROSPECT, company_name="x").kind is EntityKind.PROSPECT
    assert FakeWeb  # fixtures importable for other suites


async def test_unresolvable_domain_never_yields_fake_zero_content(web):
    """Regression (found by the Node contract test): DNS failure must not let content report '0 articles'."""

    async def nxdomain(host):
        raise OSError("NXDOMAIN")

    web.resolver = nxdomain  # type: ignore[method-assign]
    eng = make_engine(web)
    r = await eng.service.analyze_prospect({"company_name": "Nowhere", "domain": "nowhere-zt.com"}, {"max_competitors": 0})
    st = r.provider_status[r.prospect.entity_id]
    assert st["website"] is ProviderStatus.UNAVAILABLE and st["content"] is ProviderStatus.UNAVAILABLE
    assert r.observations == [] and r.status is ResearchStatus.FAILED
    await eng.aclose()


def test_node_mcp_client_contract():
    """Spec §43: the official JS MCP SDK (as ZTech's Electron main process would use it) drives the server."""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    node_dir = Path(__file__).resolve().parents[1] / "clients" / "node"
    if not shutil.which("node") or not (node_dir / "node_modules" / "@modelcontextprotocol" / "sdk").exists():
        pytest.skip("node or @modelcontextprotocol/sdk not installed (cd clients/node && npm install)")
    out = subprocess.run(
        ["node", "mcp_client_test.mjs"],
        cwd=node_dir,
        env={**__import__("os").environ, "PYTHON": sys.executable},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["ok"] and len(result["passed"]) == 6
