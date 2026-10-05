"""Deterministic extraction + stable identity (spec #25, #32, #33)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fakes import days_ago, rss, sitemap

from ztech_oi.domain.identity import (
    entity_id,
    entity_key,
    evidence_id,
    freshness_of,
    normalize_domain,
    normalize_name,
    utcnow,
)
from ztech_oi.domain.taxonomy import FreshnessState, PageKind
from ztech_oi.net.parsing import (
    classify_url,
    detect_technologies,
    extract_ctas,
    extract_emails,
    extract_prices,
    extract_socials,
    is_article_url,
    parse_feed,
    parse_html,
    parse_robots,
    parse_sitemap,
    twitter_handle,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://WWW.Example.com/path?q=1", "example.com"),
        ("example.co.uk", "example.co.uk"),
        ("sub.example.com", "sub.example.com"),
        ("localhost", ""),
        ("not a domain", ""),
        ("http://1.2.3.4", ""),
        ("", ""),
        ("münchen.de", "xn--mnchen-3ya.de"),
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


def test_normalize_name_strips_legal_suffix():
    assert normalize_name("  ACME   Bakery, Inc. ") == "acme bakery"


def test_entity_ids_are_stable_across_runs():
    k1 = entity_key(domain="https://www.acme.com", company_name="ACME")
    k2 = entity_key(domain="acme.com", company_name="Acme Bakery LLC")
    assert k1 == k2 == "dom:acme.com"
    assert entity_id(k1) == entity_id(k2)
    assert entity_key(domain=None, company_name="Acme Inc", location="Chicago") == entity_key(
        domain="", company_name="ACME", location="chicago"
    )


def test_evidence_id_stable_within_window_and_changes_across():
    a = evidence_id(entity_key="dom:a.com", provider="website", observation_type="x", source_ref="u", capture_window="2026-10-01")
    b = evidence_id(entity_key="dom:a.com", provider="website", observation_type="x", source_ref="u", capture_window="2026-10-01")
    c = evidence_id(entity_key="dom:a.com", provider="website", observation_type="x", source_ref="u", capture_window="2026-10-02")
    assert a == b != c


def test_freshness_states():
    now = utcnow()
    assert freshness_of(now - timedelta(days=1), now) is FreshnessState.FRESH
    assert freshness_of(now - timedelta(days=10), now) is FreshnessState.STALE
    assert freshness_of(now - timedelta(days=40), now) is FreshnessState.EXPIRED
    assert freshness_of("garbage", now) is FreshnessState.UNKNOWN


@pytest.mark.parametrize(
    "url,kind",
    [
        ("https://a.com/", PageKind.HOMEPAGE),
        ("https://a.com/pricing", PageKind.PRICING),
        ("https://a.com/plans/", PageKind.PRICING),
        ("https://a.com/careers", PageKind.CAREERS),
        ("https://a.com/blog/how-to-x", PageKind.BLOG),
        ("https://a.com/case-studies/acme", PageKind.CASE_STUDY),
        ("https://a.com/offers/spring", PageKind.LANDING),
        ("https://a.com/services", PageKind.SERVICE),
        ("https://a.com/products/cake", PageKind.PRODUCT),
        ("https://a.com/privacy-policy", PageKind.LEGAL),
        ("https://a.com/xyz", PageKind.OTHER),
        ("https://a.com/locations/dallas", PageKind.LOCATION),
    ],
)
def test_classify_url(url, kind):
    assert classify_url(url) is kind


def test_article_url_detection():
    assert is_article_url("https://a.com/blog/wedding-cake-ideas")
    assert not is_article_url("https://a.com/blog/")
    assert not is_article_url("https://a.com/blog/tag/cakes")
    assert not is_article_url("https://a.com/pricing")


def test_robots_parsing_and_rules():
    r = parse_robots(
        "User-agent: *\nDisallow: /private\nAllow: /private/ok\n\nUser-agent: other\nDisallow: /\nSitemap: https://a.com/sm.xml"
    )
    assert r.sitemaps == ["https://a.com/sm.xml"]
    assert not r.allowed("https://a.com/private/x")
    assert r.allowed("https://a.com/private/ok/1")
    assert r.allowed("https://a.com/public")


def test_sitemap_parsing_index_and_urls():
    entries, children = parse_sitemap(sitemap([("https://a.com/x", days_ago(3)), ("https://a.com/y", None)]).encode())
    assert [e.url for e in entries] == ["https://a.com/x", "https://a.com/y"] and entries[0].lastmod and not children
    idx = b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://a.com/s1.xml</loc></sitemap></sitemapindex>'
    assert parse_sitemap(idx) == ([], ["https://a.com/s1.xml"])


@pytest.mark.parametrize("bad", [b"<urlset><url><loc>x", b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset/>'])
def test_sitemap_malformed_or_entities_rejected(bad):
    with pytest.raises(ValueError):
        parse_sitemap(bad)


def test_feed_parsing_rss_and_atom():
    items = parse_feed(rss([("T1", "https://a.com/blog/t1", days_ago(2))]).encode())
    assert items[0].title == "T1" and items[0].published_at
    atom = (
        b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title><link href="https://a.com/blog/a"/>'
        b"<published>2026-09-01T10:00:00Z</published></entry></feed>"
    )
    assert parse_feed(atom)[0].url == "https://a.com/blog/a"


def test_html_extraction():
    html = (
        "<html><head><title> Acme </title><meta name='description' content='Best cakes'>"
        "<meta property='article:published_time' content='2026-09-01T00:00:00Z'>"
        "<link rel='alternate' type='application/rss+xml' href='/feed'>"
        '<script type=\'application/ld+json\'>{"@type":"Bakery","telephone":"+1 312 555 0100",'
        '"sameAs":["https://instagram.com/acme"]}</script></head><body>'
        "<a href='/pricing'>Pricing</a><a href='mailto:Hi@acme.com'>mail</a><a href='https://x.com/acmecakes'>x</a>"
        "<a href='/book'>Book a table</a><p>Plans from $49/month or Rs 5,000</p>"
        "<script src='https://cdn.shopify.com/s/x.js'></script></body></html>"
    )
    p = parse_html(html, "https://acme.com/")
    assert p.title == "Acme" and p.description == "Best cakes" and p.feeds == ["https://acme.com/feed"]
    assert p.published_at.startswith("2026-09-01")
    assert extract_emails(p, "acme.com") == ["hi@acme.com"]
    assert extract_socials(p) == {"x": "https://x.com/acmecakes", "instagram": "https://instagram.com/acme"}
    assert twitter_handle("https://x.com/acmecakes") == "acmecakes"
    assert "Book a table" in extract_ctas(p)
    assert {"$49/month", "Rs 5,000"} <= set(extract_prices(p.text))
    assert detect_technologies(html) == ["Shopify"]
    assert "script" not in p.text.lower() or "shopify" not in p.text.lower()
