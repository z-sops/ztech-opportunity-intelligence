"""Offline test doubles: a fake internet (httpx MockTransport), DNS, search and LLM.

Fake sites are generated relative to "now" so 30/60/90-day windows are stable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ztech_oi.integrations.search import SearchResult

PUBLIC_IP = "93.184.216.34"


def days_ago(n: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=n)


@dataclass
class Route:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


class FakeWeb:
    """Routes keyed by 'host/path' (no scheme, no www, no query)."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.calls: list[str] = []
        self.dns: dict[str, list[str]] = {}

    def add(
        self,
        url: str,
        body: str | bytes | dict | list = "",
        status: int = 200,
        ctype: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if isinstance(body, (dict, list)):
            data, ct = json.dumps(body).encode(), "application/json"
        elif isinstance(body, str):
            data, ct = body.encode(), "text/html; charset=utf-8"
        else:
            data, ct = body, "application/octet-stream"
        h = {"content-type": ctype or ct, **(headers or {})}
        self.routes[_key(url)] = Route(status, data, h)

    def add_fn(self, url: str, fn) -> None:
        self.routes[_key(url)] = fn

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        r = self.routes.get(_key(str(request.url)))
        if callable(r):
            r = r(request)
        if r is None:
            return httpx.Response(404, content=b"not found", headers={"content-type": "text/html"})
        return httpx.Response(r.status, content=r.body, headers=r.headers)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    async def resolver(self, host: str) -> list[str]:
        if host in self.dns:
            return self.dns[host]
        return [PUBLIC_IP]

    def called(self, fragment: str) -> int:
        return sum(1 for c in self.calls if fragment in c)


def _key(url: str) -> str:
    p = urlsplit(url)
    host = (p.hostname or "").removeprefix("www.")
    path = p.path or "/"
    return f"{host}{path}"


# --------------------------------------------------------------- site builder
def html_page(title: str, body: str, *, description: str = "", head: str = "") -> str:
    return (
        f"<html><head><title>{title}</title><meta name='description' content='{description}'>{head}</head>"
        f"<body>{body}</body></html>"
    )


def sitemap(urls: list[tuple[str, datetime | None]]) -> str:
    rows = "".join(f"<url><loc>{u}</loc>{f'<lastmod>{d.date().isoformat()}</lastmod>' if d else ''}</url>" for u, d in urls)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</urlset>'


def rss(items: list[tuple[str, str, datetime]]) -> str:
    rows = "".join(f"<item><title>{t}</title><link>{u}</link><pubDate>{format_datetime(d)}</pubDate></item>" for t, u, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{rows}</channel></rss>'


def build_site(
    web: FakeWeb,
    domain: str,
    *,
    name: str,
    articles_days: list[int],
    landing: int = 0,
    pricing: bool = True,
    careers: bool = False,
    extra_pages: list[str] | None = None,
    with_sitemap: bool = True,
    with_feed: bool = True,
    x_handle: str | None = None,
    offers: str = "",
    topic: str = "wedding cakes",
) -> None:
    base = f"https://{domain}"
    pages: list[tuple[str, datetime | None]] = [
        (f"{base}/", days_ago(1)),
        (f"{base}/about", days_ago(200)),
        (f"{base}/contact", days_ago(200)),
        (f"{base}/services", days_ago(50)),
    ]
    if pricing:
        pages.append((f"{base}/pricing", days_ago(20)))
    if careers:
        pages.append((f"{base}/careers", days_ago(10)))
    for i in range(landing):
        pages.append((f"{base}/offers/landing-{i}", days_ago(5 + i)))
    arts = []
    for i, d in enumerate(articles_days):
        u = f"{base}/blog/{topic.replace(' ', '-')}-guide-{i}"
        pages.append((u, days_ago(d)))
        arts.append((f"{topic.title()} guide {i}", u, days_ago(d)))
    for p in extra_pages or []:
        pages.append((f"{base}{p}", days_ago(3)))
    socials = f"<a href='https://x.com/{x_handle}'>X</a>" if x_handle else ""
    socials += f"<a href='https://www.linkedin.com/company/{domain.split('.')[0]}'>in</a>"
    feed_link = f"<link rel='alternate' type='application/rss+xml' href='{base}/feed'>" if with_feed else ""
    org = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "Bakery",
            "name": name,
            "telephone": "+1 312 555 0100",
            "address": {"@type": "PostalAddress", "addressLocality": "Chicago", "addressRegion": "IL"},
        }
    )
    home_body = (
        f"<h1>{name}</h1><p>Fresh {topic} in Chicago. {offers}</p>"
        f"<a href='/about'>About</a><a href='/services'>Services</a><a href='/contact'>Contact us</a>"
        f"<a href='/pricing'>Pricing</a><a href='/order'>Order now</a><a href='mailto:hello@{domain}'>Email</a>"
        f"<a href='tel:+13125550100'>Call</a>{socials}"
        "<script src='https://connect.facebook.net/en_US/fbevents.js'></script>"
        "<script async src='https://www.googletagmanager.com/gtm.js?id=GTM-X'></script>"
    )
    web.add(
        f"{base}/",
        html_page(
            name,
            home_body,
            description=f"{name} — {topic} bakery in Chicago",
            head=feed_link + f"<script type='application/ld+json'>{org}</script>",
        ),
    )
    web.add(f"{base}/robots.txt", f"User-agent: *\nDisallow: /admin\nSitemap: {base}/sitemap.xml\n", ctype="text/plain")
    if with_sitemap:
        web.add(f"{base}/sitemap.xml", sitemap(pages), ctype="application/xml")
    if with_feed:
        web.add(f"{base}/feed", rss(arts), ctype="application/rss+xml")
    web.add(
        f"{base}/pricing",
        html_page(
            "Pricing",
            "<h1>Pricing</h1><p>Wedding cake from $299. Cupcakes $3.50 per person. Free delivery on orders over $100.</p>",
        ),
    )
    web.add(
        f"{base}/services",
        html_page("Services", "<h1>Services</h1><h2>Wedding cakes</h2><h2>Corporate catering</h2><h2>Custom birthday cakes</h2>"),
    )
    web.add(f"{base}/contact", html_page("Contact", f"<h1>Contact</h1><a href='mailto:orders@{domain}'>orders</a>"))
    web.add(f"{base}/about", html_page("About", "<h1>About us</h1><p>Family bakery since 1998.</p>"))
    if careers:
        web.add(f"{base}/careers", html_page("Careers", "<h1>Join us</h1><p>We are hiring bakers.</p>"))
    for u, _t, d in [(a[1], a[0], a[2]) for a in arts]:
        web.add(
            u,
            html_page(
                "Article",
                f"<article><time datetime='{d.isoformat()}'></time><p>text</p></article>",
                head=f"<meta property='article:published_time' content='{d.isoformat()}'>",
            ),
        )


# ---------------------------------------------------------------- search/llm
class FakeSearch:
    name = "fake_search"
    configured = True

    def __init__(
        self, results: dict[str, list[SearchResult]] | None = None, default: list[SearchResult] | None = None, error=None
    ) -> None:
        self.results = results or {}
        self.default = default or []
        self.queries: list[str] = []
        self.error = error

    async def search(self, query: str, *, count: int = 10, country: str | None = None) -> list[SearchResult]:
        self.queries.append(query)
        if self.error:
            raise self.error
        for k, v in self.results.items():
            if k.lower() in query.lower():
                return v
        return self.default


class FakeLLM:
    name = "fake_llm"
    configured = True

    def __init__(self, reply: dict | None = None, by_marker: dict[str, dict] | None = None) -> None:
        self.reply = reply
        self.by_marker = by_marker or {}
        self.calls = 0

    async def json_completion(self, system: str, user: str, *, max_tokens: int = 800) -> dict | None:
        self.calls += 1
        for marker, rep in self.by_marker.items():
            if marker in system:
                return rep
        return self.reply


def sr(url: str, title: str, snippet: str = "", rank: int = 1) -> SearchResult:
    return SearchResult(url=url, title=title, snippet=snippet, rank=rank)


def meta_payload(page_name: str, n: int, *, start_days: int = 10, prefix: str = "a") -> dict:
    return {
        "data": [
            {
                "id": f"{prefix}{i}",
                "page_id": "1",
                "page_name": page_name,
                "ad_delivery_start_time": days_ago(start_days + i).strftime("%Y-%m-%d"),
                "ad_creative_bodies": [f"Order your wedding cake today - 20% off tastings ({i})"],
                "ad_creative_link_titles": ["Book a tasting"],
                "ad_creative_link_captions": ["example.com"],
                "ad_snapshot_url": f"https://www.facebook.com/ads/archive/render_ad/?id={i}&access_token=SECRET123",
                "publisher_platforms": ["facebook", "instagram"],
            }
            for i in range(n)
        ]
    }


def query(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(request.url)).query)
