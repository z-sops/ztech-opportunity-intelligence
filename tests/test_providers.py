"""Provider contract tests (spec #26, #27, #38): every provider returns a valid
ProviderResult envelope with an honest status for every scenario."""

from __future__ import annotations

import asyncio
import json

import pytest
from fakes import FakeLLM, FakeSearch, FakeWeb, Route, build_site, days_ago, meta_payload, query, sr

from ztech_oi.domain.errors import FetchError
from ztech_oi.domain.identity import entity_id, entity_key
from ztech_oi.domain.models import Entity, ProviderResult
from ztech_oi.domain.taxonomy import EntityKind, ErrorCode, ObservationType, ProviderStatus
from ztech_oi.net.http import SafeHttpClient
from ztech_oi.providers.ads import GoogleAdsProvider, MetaAdsProvider, name_match
from ztech_oi.providers.base import ResearchRequest, run_provider
from ztech_oi.providers.competitors import CompetitorDiscoveryProvider
from ztech_oi.providers.content import ContentProvider
from ztech_oi.providers.social import LinkedInProvider, TwitterProvider
from ztech_oi.providers.website import WebsiteProvider
from ztech_oi.security.url_guard import UrlGuard


def ent(name="SweetCrumb Bakery", domain: str | None = "sweetcrumb.com", kind=EntityKind.PROSPECT) -> Entity:
    k = entity_key(domain=domain, company_name=name)
    return Entity(
        entity_id=entity_id(k), entity_key=k, kind=kind, company_name=name, domain=domain, location="Chicago", industry="Bakery"
    )


def req(e: Entity | None = None, **ctx) -> ResearchRequest:
    e = e or ent()
    return ResearchRequest(
        research_id="res_test", entity=e, prospect=e, is_prospect=True, context=dict(ctx), products_services=["wedding cakes"]
    )


def http(web: FakeWeb) -> SafeHttpClient:
    return SafeHttpClient(
        UrlGuard(resolver=web.resolver, allow_hosts={"graph.facebook.com", "serpapi.com", "api.x.com"}),
        transport=web.transport(),
        per_host_interval_s=0,
        backoff_base_s=0,
        max_retries=1,
    )


def check_envelope(r: ProviderResult) -> None:
    ProviderResult.model_validate_json(r.model_dump_json())  # round-trips, JSON-serialisable
    assert r.telemetry.provider == r.provider and r.telemetry.status == r.status
    assert r.telemetry.evidence_created == len(r.evidence)
    ids = {e.evidence_id for e in r.evidence}
    for o in r.observations:
        assert set(o.evidence_refs) <= ids, "observation must only reference evidence it produced"


# ------------------------------------------------------------------ website
async def test_website_success_inventory_profile(web):
    build_site(web, "sweetcrumb.com", name="SweetCrumb Bakery", articles_days=[3, 40], careers=True, landing=2)
    r = req()
    res = await WebsiteProvider(http(web)).collect(r)
    check_envelope(res)
    assert res.status is ProviderStatus.SUCCESS
    inv = next(o for o in res.observations if o.type is ObservationType.WEBSITE_PAGE_INVENTORY)
    assert inv.metrics["inventory_from_sitemap"] == 1 and inv.metrics["careers_page_count"] == 1
    assert inv.metrics["landing_page_count"] == 2 and inv.metrics["pricing_page_count"] == 1
    prof = next(o for o in res.observations if o.type is ObservationType.WEBSITE_COMPANY_PROFILE).items[0]
    assert "hello@sweetcrumb.com" in prof["emails"] and prof["phones"] == ["+13125550100"]
    assert "Wedding cakes" in prof["services"] and "$299" in prof["prices"]
    assert prof["locations"] == ["Chicago, IL"] and "Meta Pixel" in prof["technologies"]
    # every profile field has provenance
    assert set(prof["field_evidence"]) >= {"emails", "phones", "services", "prices", "technologies"}
    assert r.context["sitemap_entries"] and r.context["site_reachable"] is True


async def test_website_respects_robots_disallow(web):
    build_site(web, "sweetcrumb.com", name="S", articles_days=[])
    web.add(
        "https://sweetcrumb.com/robots.txt",
        "User-agent: *\nDisallow: /pricing\nSitemap: https://sweetcrumb.com/sitemap.xml",
        ctype="text/plain",
    )
    res = await WebsiteProvider(http(web)).collect(req())
    assert web.called("sweetcrumb.com/pricing") == 0
    assert any("disallowed by robots.txt" in x for x in res.limitations)


async def test_website_no_domain_is_unavailable(web):
    res = await WebsiteProvider(http(web)).collect(req(ent(domain=None)))
    check_envelope(res)
    assert res.status is ProviderStatus.UNAVAILABLE and res.errors[0].code is ErrorCode.INSUFFICIENT_EVIDENCE


async def test_website_homepage_down_is_unavailable_not_fake(web):
    web.add("https://sweetcrumb.com/", "boom", status=503)
    r = req()
    res = await WebsiteProvider(http(web)).collect(r)
    check_envelope(res)
    assert res.status is ProviderStatus.UNAVAILABLE and not res.observations and not res.evidence
    assert r.context["site_reachable"] is False


async def test_website_private_dns_is_blocked(web):
    web.dns["sweetcrumb.com"] = ["10.0.0.1"]
    res = await WebsiteProvider(http(web)).collect(req())
    assert res.status is ProviderStatus.UNAVAILABLE and res.errors[0].code is ErrorCode.SSRF_BLOCKED
    assert not web.calls


async def test_website_malformed_sitemap_falls_back_honestly(web):
    build_site(web, "sweetcrumb.com", name="S", articles_days=[])
    web.add("https://sweetcrumb.com/sitemap.xml", "<urlset><url><loc>broken", ctype="application/xml")
    res = await WebsiteProvider(http(web)).collect(req())
    inv = next(o for o in res.observations if o.type is ObservationType.WEBSITE_PAGE_INVENTORY)
    assert inv.metrics["inventory_from_sitemap"] == 0
    assert any("MALFORMED_RESPONSE" in x for x in res.limitations)


async def test_website_huge_page_is_reported_not_crashed(web):
    web.add("https://sweetcrumb.com/", "<html>" + "x" * 4_000_000 + "</html>")
    res = await WebsiteProvider(http(web)).collect(req())
    assert res.status is ProviderStatus.UNAVAILABLE and res.errors[0].code is ErrorCode.RESPONSE_TOO_LARGE


# ------------------------------------------------------------------ content
async def test_content_windows_from_feed(web):
    build_site(web, "rival.com", name="Rival", articles_days=[2, 10, 25, 45, 80, 200])
    r = req(ent("Rival", "rival.com"))
    await WebsiteProvider(http(web)).collect(r)
    res = await ContentProvider(http(web)).collect(r)
    check_envelope(res)
    m = next(o for o in res.observations if o.type is ObservationType.CONTENT_INVENTORY).metrics
    assert (m["articles_last_30d"], m["articles_last_60d"], m["articles_last_90d"]) == (3, 4, 5)
    assert m["from_feed"] == 1 and m["relevant_articles_last_60d"] == 4


async def test_content_without_feed_uses_page_dates(web):
    build_site(web, "rival.com", name="Rival", articles_days=[5, 20, 100], with_feed=False)
    r = req(ent("Rival", "rival.com"))
    await WebsiteProvider(http(web)).collect(r)
    res = await ContentProvider(http(web)).collect(r)
    m = next(o for o in res.observations if o.type is ObservationType.CONTENT_INVENTORY).metrics
    assert m["articles_last_60d"] == 2 and m["from_feed"] == 0
    assert any("No RSS/Atom feed" in x for x in res.limitations)


async def test_content_undated_articles_give_null_not_zero(web):
    build_site(web, "rival.com", name="Rival", articles_days=[5, 20], with_feed=False)
    for i in range(2):
        web.add(f"https://rival.com/blog/wedding-cakes-guide-{i}", "<html><body>no dates</body></html>")
    r = req(ent("Rival", "rival.com"))
    await WebsiteProvider(http(web)).collect(r)
    res = await ContentProvider(http(web)).collect(r)
    m = next(o for o in res.observations if o.type is ObservationType.CONTENT_INVENTORY).metrics
    assert m["article_count_observed"] == 2 and m["articles_last_60d"] is None


async def test_content_no_articles_is_partial(web):
    build_site(web, "plain.com", name="Plain", articles_days=[], with_feed=False)
    r = req(ent("Plain", "plain.com"))
    await WebsiteProvider(http(web)).collect(r)
    res = await ContentProvider(http(web)).collect(r)
    assert res.status is ProviderStatus.PARTIAL
    assert next(o for o in res.observations).metrics["articles_last_60d"] is None


async def test_content_llm_themes_are_inference_and_grounded(web):
    build_site(web, "rival.com", name="Rival", articles_days=[2, 10, 25])
    llm = FakeLLM(
        {
            "themes": [
                {"theme": "Weddings", "title_indexes": [1, 2], "confidence": 0.99},
                {"theme": "Invented", "title_indexes": [99], "confidence": 0.9},
            ]
        }
    )
    r = req(ent("Rival", "rival.com"))
    await WebsiteProvider(http(web)).collect(r)
    res = await ContentProvider(http(web), llm).collect(r)
    themes = [e for e in res.evidence if e.observation_type is ObservationType.CONTENT_TOPICS]
    assert len(themes) == 1 and themes[0].claim_kind.value == "inference" and themes[0].confidence <= 0.75


# -------------------------------------------------------------------- meta
async def test_meta_not_configured(web):
    res = await MetaAdsProvider(http(web), None, ["US"]).collect(req())
    check_envelope(res)
    assert res.status is ProviderStatus.UNAVAILABLE and res.errors[0].code is ErrorCode.PROVIDER_NOT_CONFIGURED
    assert any("NOT CONFIGURED" in x for x in res.limitations)


async def test_meta_success_entity_matching_and_token_stripped(web):
    payload = meta_payload("SweetCrumb Bakery", 3)
    payload["data"] += meta_payload("Totally Different Shop", 2, prefix="z")["data"]
    seen = {}

    def handler(request):
        seen.update(query(request))
        return Route(200, json.dumps(payload).encode(), {"content-type": "application/json"})

    web.add_fn("https://graph.facebook.com/v21.0/ads_archive", handler)
    res = await MetaAdsProvider(http(web), "TOKEN123", ["DE", "FR"]).collect(req())
    check_envelope(res)
    obs = next(o for o in res.observations if o.type is ObservationType.ADS_META)
    assert obs.metrics["active_creative_count"] == 3 and obs.metrics["coverage_reliable"] == 1
    assert json.loads(seen["ad_reached_countries"][0]) == ["DE", "FR"]
    dump = res.model_dump_json()
    assert "TOKEN123" not in dump and "SECRET123" not in dump
    assert not any("spend" in e.claim.lower() and "$" in e.claim for e in res.evidence)


async def test_meta_zero_outside_eu_is_marked_unreliable(web):
    web.add("https://graph.facebook.com/v21.0/ads_archive", {"data": []})
    res = await MetaAdsProvider(http(web), "T", ["US"]).collect(req())
    obs = next(o for o in res.observations if o.type is ObservationType.ADS_META)
    assert obs.metrics["active_creative_count"] == 0 and obs.metrics["coverage_reliable"] == 0
    assert any("NOT evidence of no advertising" in x for x in res.limitations)


@pytest.mark.parametrize(
    "code,status,err",
    [
        (17, ProviderStatus.RATE_LIMITED, ErrorCode.RATE_LIMITED),
        (190, ProviderStatus.UNAVAILABLE, ErrorCode.PROVIDER_NOT_CONFIGURED),
        (1, ProviderStatus.FAILED, ErrorCode.SOURCE_UNAVAILABLE),
    ],
)
async def test_meta_api_errors(web, code, status, err):
    web.add("https://graph.facebook.com/v21.0/ads_archive", {"error": {"code": code, "message": "x"}}, status=400)
    res = await MetaAdsProvider(http(web), "T", ["DE"]).collect(req())
    check_envelope(res)
    assert res.status is status and res.errors[0].code is err


async def test_meta_malformed_response(web):
    web.add("https://graph.facebook.com/v21.0/ads_archive", {"data": "not-a-list"})
    res = await MetaAdsProvider(http(web), "T", ["DE"]).collect(req())
    assert res.status is ProviderStatus.FAILED and res.errors[0].code is ErrorCode.MALFORMED_RESPONSE


def test_name_match_scale():
    assert name_match("SweetCrumb Bakery", "Sweetcrumb Bakery") == 0.95
    assert name_match("SweetCrumb Bakery", "SweetCrumb Bakery Chicago") == 0.75
    assert name_match("SweetCrumb Bakery", "Other Shop") == 0.0


# ------------------------------------------------------------------ google
async def test_google_not_configured(web):
    res = await GoogleAdsProvider(http(web), None).collect(req())
    assert res.status is ProviderStatus.UNAVAILABLE and res.errors[0].code is ErrorCode.PROVIDER_NOT_CONFIGURED


async def test_google_via_serpapi_marks_third_party(web):
    ts = int(days_ago(5).timestamp())
    web.add(
        "https://serpapi.com/search.json",
        {
            "ad_creatives": [
                {
                    "advertiser": "SweetCrumb Bakery",
                    "ad_creative_id": "CR1",
                    "format": "text",
                    "target_domain": "sweetcrumb.com",
                    "first_shown": ts,
                    "last_shown": ts,
                },
                {"advertiser": "Unrelated LLC", "ad_creative_id": "CR2", "format": "image", "target_domain": "other.com"},
            ]
        },
    )
    res = await GoogleAdsProvider(http(web), "K").collect(req())
    check_envelope(res)
    obs = res.observations[0]
    assert obs.metrics["creative_count"] == 1 and obs.metrics["shown_last_30d"] == 1
    assert all(e.source_type == "third_party:serpapi" for e in res.evidence)
    assert "K" not in json.dumps([e.source_url for e in res.evidence])


# ------------------------------------------------------------------ social
async def test_linkedin_is_unsupported():
    res = await LinkedInProvider().collect(req())
    check_envelope(res)
    assert res.status is ProviderStatus.UNSUPPORTED and not res.evidence


async def test_twitter_needs_token_and_handle(web):
    assert (await TwitterProvider(http(web), None).collect(req())).status is ProviderStatus.UNAVAILABLE
    res = await TwitterProvider(http(web), "B").collect(req(social_profiles={}))
    assert res.status is ProviderStatus.UNAVAILABLE and res.errors[0].code is ErrorCode.INSUFFICIENT_EVIDENCE


async def test_twitter_success(web):
    web.add(
        "https://api.x.com/2/users/by/username/sweetcrumb", {"data": {"id": "42", "public_metrics": {"followers_count": 900}}}
    )
    web.add(
        "https://api.x.com/2/users/42/tweets",
        {
            "data": [
                {"id": "1", "text": "New wedding menu", "created_at": days_ago(3).isoformat()},
                {"id": "2", "text": "old", "created_at": days_ago(90).isoformat()},
            ]
        },
    )
    res = await TwitterProvider(http(web), "B").collect(req(social_profiles={"x": "https://x.com/sweetcrumb"}))
    check_envelope(res)
    assert res.observations[0].metrics["posts_last_30d"] == 1 and res.observations[0].metrics["followers"] == 900


async def test_twitter_paywalled_tier(web):
    web.add("https://api.x.com/2/users/by/username/sweetcrumb", {"title": "Forbidden"}, status=403)
    res = await TwitterProvider(http(web), "B").collect(req(social_profiles={"x": "https://x.com/sweetcrumb"}))
    assert res.status is ProviderStatus.UNAVAILABLE and res.errors[0].code is ErrorCode.PROVIDER_NOT_CONFIGURED


# --------------------------------------------------------------- discovery
def _items(res):
    return next(o for o in res.observations if o.type is ObservationType.COMPETITOR_CANDIDATES).items


async def test_discovery_without_search_or_hints_is_unavailable():
    from ztech_oi.integrations.search import NullSearch

    res = await CompetitorDiscoveryProvider(NullSearch()).collect(req())
    check_envelope(res)
    assert res.status is ProviderStatus.UNAVAILABLE and res.errors[0].code is ErrorCode.PROVIDER_NOT_CONFIGURED


async def test_discovery_hints_dedup_and_self_exclusion():
    from ztech_oi.integrations.search import NullSearch

    hints = [
        {"company_name": "Rival Bakes", "domain": "rivalbakes.com"},
        {"company_name": "Rival Bakes (dup)", "domain": "https://www.rivalbakes.com/"},
        {"company_name": "SweetCrumb Bakery", "domain": "sweetcrumb.com"},
    ]
    res = await CompetitorDiscoveryProvider(NullSearch()).collect(req(known_competitors=hints))
    items = _items(res)
    assert res.status is ProviderStatus.PARTIAL
    assert [i["domain"] for i in items] == ["rivalbakes.com"] and items[0]["relationship_type"] == "user_provided"
    assert any("matches the prospect itself" in x for x in res.limitations)


async def test_discovery_search_excludes_directories_and_prospect():
    search = FakeSearch(
        default=[
            sr("https://www.yelp.com/biz/x", "Yelp"),
            sr("https://sweetcrumb.com/", "SweetCrumb"),
            sr("https://rivalbakes.com/", "Rival Bakes | Cakes"),
            sr("https://en.wikipedia.org/wiki/Cake", "Cake"),
        ]
    )
    res = await CompetitorDiscoveryProvider(search).collect(req())
    check_envelope(res)
    items = _items(res)
    assert [i["domain"] for i in items] == ["rivalbakes.com"]
    assert items[0]["relationship_type"] == "likely_competitor"  # appeared for >=2 queries
    assert all(e.claim_kind.value == "inference" for e in res.evidence)


async def test_discovery_llm_hallucinations_are_dropped():
    search = FakeSearch(default=[sr("https://rivalbakes.com/", "Rival Bakes | Wedding cakes", "Rival Bakes Chicago")])
    llm = FakeLLM(
        {
            "competitors": [
                {
                    "company_name": "Rival Bakes",
                    "result_index": 1,
                    "relationship_type": "direct_competitor",
                    "confidence": 0.99,
                    "reason": "same",
                },
                {
                    "company_name": "Ghost Cakes",
                    "result_index": 1,
                    "relationship_type": "direct_competitor",
                    "confidence": 0.9,
                    "reason": "?",
                },
                {
                    "company_name": "Out Of Range",
                    "result_index": 7,
                    "relationship_type": "direct_competitor",
                    "confidence": 0.9,
                    "reason": "?",
                },
            ]
        }
    )
    res = await CompetitorDiscoveryProvider(search, llm).collect(req())
    items = _items(res)
    assert [i["company_name"] for i in items] == ["Rival Bakes"]
    assert items[0]["relationship_confidence"] <= 0.85 and items[0]["discovered_via"] == "search+llm"


async def test_discovery_llm_listicle_resolves_official_domain():
    search = FakeSearch(
        results={
            "official site": [sr("https://cakehouse.com/", "Cake House official")],
        },
        default=[sr("https://bestofchicago.example-blog.com/top", "Top bakeries", "Cake House is a top bakery")],
    )
    llm = FakeLLM(
        {
            "competitors": [
                {
                    "company_name": "Cake House",
                    "result_index": 1,
                    "relationship_type": "likely_competitor",
                    "confidence": 0.8,
                    "reason": "listed",
                }
            ]
        }
    )
    res = await CompetitorDiscoveryProvider(search, llm).collect(req())
    assert [i["domain"] for i in _items(res)] == ["cakehouse.com"]


async def test_discovery_search_rate_limited():
    search = FakeSearch(error=FetchError(ErrorCode.RATE_LIMITED, "429"))
    res = await CompetitorDiscoveryProvider(search).collect(req())
    assert res.status is ProviderStatus.RATE_LIMITED


# ---------------------------------------------------------------- run_provider
class _Slow:
    name, source_type, scope = "slow", "x", "per_entity"

    async def collect(self, request):
        await asyncio.sleep(5)


class _Crash:
    name, source_type, scope = "crash", "x", "per_entity"

    async def collect(self, request):
        raise RuntimeError("bug in provider")


async def test_run_provider_timeout_and_crash_never_raise():
    t = await run_provider(_Slow(), req(), timeout_s=0.05)
    assert t.status is ProviderStatus.FAILED and t.errors[0].code is ErrorCode.TIMEOUT
    c = await run_provider(_Crash(), req(), timeout_s=1)
    assert c.status is ProviderStatus.FAILED and c.errors[0].code is ErrorCode.PROVIDER_FAILED
    check_envelope(t)
    check_envelope(c)
