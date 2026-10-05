"""Website Intelligence Provider (spec #9, #15) — REAL, no API key needed.

Pipeline (bounded at every step):
  1. robots.txt  -> crawl rules + declared sitemaps (respected for page fetches)
  2. homepage    -> title/description, links, feeds, JSON-LD, tech signatures, CTAs
  3. sitemap(s)  -> full page inventory with lastmod (falls back to homepage links)
  4. up to N key pages (pricing, services/products, contact, about, careers, locations)
                 -> services/products, prices, offers, contact facts, locations
Outputs three structured observations:
  website.homepage, website.page_inventory, website.company_profile
and shares sitemap entries / feeds / social handles with later providers via
request.context.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from urllib.parse import urlsplit

from ..domain.errors import FetchError, UnsafeTarget
from ..domain.models import ProviderResult
from ..domain.taxonomy import COMMERCIAL_PAGE_KINDS, ClaimKind, ErrorCode, ObservationType, PageKind, ProviderStatus
from ..net.http import SafeHttpClient
from ..net.parsing import (
    ParsedPage,
    Robots,
    SitemapEntry,
    classify_url,
    detect_technologies,
    extract_ctas,
    extract_emails,
    extract_offers,
    extract_phones,
    extract_prices,
    extract_socials,
    jsonld_locations,
    jsonld_org,
    parse_html,
    parse_robots,
    parse_sitemap,
    same_site,
)
from .base import ResearchRequest, ResultBuilder, simple_result

log = logging.getLogger("ztech_oi.providers.website")

KEY_PAGE_ORDER = [
    PageKind.PRICING,
    PageKind.SERVICE,
    PageKind.PRODUCT,
    PageKind.CONTACT,
    PageKind.ABOUT,
    PageKind.CAREERS,
    PageKind.LOCATION,
    PageKind.LANDING,
]


class WebsiteProvider:
    name = "website"
    source_type = "website"
    scope = "per_entity"

    def __init__(
        self,
        http: SafeHttpClient,
        *,
        max_pages: int = 8,
        max_sitemap_urls: int = 2000,
        max_sitemap_files: int = 5,
        max_inventory_items: int = 300,
    ) -> None:
        self.http = http
        self.max_pages = max_pages
        self.max_sitemap_urls = max_sitemap_urls
        self.max_sitemap_files = max_sitemap_files
        self.max_inventory_items = max_inventory_items

    async def collect(self, request: ResearchRequest) -> ProviderResult:
        domain = request.entity.domain
        if not domain:
            return simple_result(
                self.name,
                self.source_type,
                request,
                ProviderStatus.UNAVAILABLE,
                ErrorCode.INSUFFICIENT_EVIDENCE,
                "entity has no domain; website intelligence needs a domain",
            )
        b = ResultBuilder(self.name, self.source_type, request)
        b.limit("Static HTML only: content rendered exclusively by client-side JavaScript is not observed.")

        # 1) robots.txt
        robots = Robots()
        try:
            r = await self.http.get(f"https://{domain}/robots.txt")
            if r.ok:
                robots = parse_robots(r.text)
        except (FetchError, UnsafeTarget) as e:
            if e.code in (ErrorCode.SSRF_BLOCKED, ErrorCode.INVALID_DOMAIN):
                request.context["site_reachable"] = False
                return simple_result(self.name, self.source_type, request, ProviderStatus.UNAVAILABLE, e.code, e.message)
            b.limit("robots.txt could not be read; default crawl rules applied conservatively.")

        # 2) homepage
        try:
            home_resp = await self.http.get(f"https://{domain}/")
        except (FetchError, UnsafeTarget) as e:
            b.error(e.code, f"homepage unreachable: {e.message}")
            request.context["site_reachable"] = False
            status = ProviderStatus.RATE_LIMITED if e.code is ErrorCode.RATE_LIMITED else ProviderStatus.UNAVAILABLE
            return b.build(status)
        if not home_resp.ok or not home_resp.content:
            b.error(ErrorCode.SOURCE_UNAVAILABLE, f"homepage returned HTTP {home_resp.status}")
            request.context["site_reachable"] = False
            return b.build(ProviderStatus.UNAVAILABLE)
        b.retry_count += home_resp.retries
        request.context["site_reachable"] = True
        final_host = (urlsplit(home_resp.final_url).hostname or domain).removeprefix("www.")
        site_domain = domain if same_site(home_resp.final_url, domain) else final_host
        if site_domain != domain:
            b.limit(f"Homepage redirected to a different domain ({site_domain}); inventory uses the final domain.")
        home = parse_html(home_resp.text, home_resp.final_url)
        home_hash = hashlib.sha256(home_resp.content).hexdigest()[:16]
        home_ev = b.add_evidence(
            observation_type=ObservationType.WEBSITE_HOMEPAGE,
            claim=f'Homepage of {domain} was retrieved (HTTP {home_resp.status}); title: "{home.title[:120]}"',
            source_ref=home_resp.final_url,
            source_url=home_resp.final_url,
            confidence=0.95,
            raw={"title": home.title, "description": home.description, "excerpt": home.text[:400]},
            content_hash=home_hash,
        )
        b.add_observation(
            ObservationType.WEBSITE_HOMEPAGE,
            metrics={
                "internal_link_count": sum(1 for lk in home.links if same_site(lk.url, site_domain)),
                "word_count": home.word_count,
            },
            items=[{"url": home_resp.final_url, "title": home.title, "description": home.description, "content_hash": home_hash}],
            evidence_refs=[home_ev],
        )

        # 3) inventory: sitemap first, homepage links as fallback
        entries, sitemap_source, sitemap_err = await self._sitemap_entries(site_domain, robots)
        inventory_source = "sitemap"
        if not entries:
            inventory_source = "homepage_links"
            if sitemap_err:
                b.limit(f"Sitemap unavailable ({sitemap_err}); inventory limited to links found on the homepage.")
            else:
                b.limit("No sitemap found; inventory limited to links found on the homepage.")
            seen: dict[str, None] = {}
            for lk in home.links:
                if same_site(lk.url, site_domain) and lk.url.startswith("http"):
                    seen.setdefault(lk.url.split("#")[0], None)
            entries = [SitemapEntry(url=u) for u in list(seen)[: self.max_sitemap_urls]]
        pages = [(e, classify_url(e.url)) for e in entries]
        kinds = Counter(k for _, k in pages)
        commercial = sum(kinds[k] for k in COMMERCIAL_PAGE_KINDS)
        inv_ev = b.add_evidence(
            observation_type=ObservationType.WEBSITE_PAGE_INVENTORY,
            claim=(
                f"{len(pages)} same-site URLs observed on {site_domain} via {inventory_source}"
                f" ({commercial} commercial, {kinds[PageKind.BLOG]} blog/news, {kinds[PageKind.PRICING]} pricing,"
                f" {kinds[PageKind.CAREERS]} careers)"
            ),
            source_ref=f"inventory:{inventory_source}:{sitemap_source or home_resp.final_url}",
            source_url=sitemap_source or home_resp.final_url,
            confidence=0.9 if inventory_source == "sitemap" else 0.7,
            metric="page_count",
            value=len(pages),
            raw={"inventory_source": inventory_source, "counts": {k.value: v for k, v in kinds.items()}},
        )
        kind_evidence: dict[PageKind, str] = {}
        for kind in (PageKind.PRICING, PageKind.CAREERS, PageKind.LANDING, PageKind.CASE_STUDY, PageKind.LOCATION):
            sample = [e.url for e, k in pages if k is kind][:5]
            if sample:
                kind_evidence[kind] = b.add_evidence(
                    observation_type=ObservationType.WEBSITE_PAGE_INVENTORY,
                    claim=f"{kinds[kind]} {kind.value} page(s) listed on {site_domain}, e.g. {sample[0]}",
                    source_ref=f"kind:{kind.value}:{sample[0]}",
                    source_url=sample[0],
                    confidence=0.75,  # URL-pattern classification
                    metric=f"{kind.value}_page_count",
                    value=kinds[kind],
                    raw={"sample_urls": sample, "classification": "url_pattern"},
                )
        b.add_observation(
            ObservationType.WEBSITE_PAGE_INVENTORY,
            metrics={
                "page_count": len(pages),
                "commercial_page_count": commercial,
                "pricing_page_count": kinds[PageKind.PRICING],
                "product_page_count": kinds[PageKind.PRODUCT],
                "service_page_count": kinds[PageKind.SERVICE],
                "landing_page_count": kinds[PageKind.LANDING],
                "case_study_page_count": kinds[PageKind.CASE_STUDY],
                "blog_page_count": kinds[PageKind.BLOG],
                "careers_page_count": kinds[PageKind.CAREERS],
                "contact_page_count": kinds[PageKind.CONTACT],
                "location_page_count": kinds[PageKind.LOCATION],
                "inventory_from_sitemap": 1 if inventory_source == "sitemap" else 0,
            },
            items=[{"url": e.url, "kind": k.value, "lastmod": e.lastmod} for e, k in pages[: self.max_inventory_items]],
            evidence_refs=[inv_ev, *kind_evidence.values()],
        )
        if len(pages) > self.max_inventory_items:
            b.limit(f"Inventory items truncated to {self.max_inventory_items} in the report (counts cover all {len(pages)}).")
        request.context["sitemap_entries"] = [{"url": e.url, "lastmod": e.lastmod, "kind": k.value} for e, k in pages]
        request.context["feeds"] = home.feeds
        request.context["site_domain"] = site_domain

        # 4) key pages -> company profile
        fetched: list[ParsedPage] = [home]
        targets: list[str] = []
        url_pool = [e.url for e, _ in pages] + [lk.url for lk in home.links if same_site(lk.url, site_domain)]
        for kind in KEY_PAGE_ORDER:
            cand = next((u for u in url_pool if classify_url(u) is kind and u not in targets), None)
            if cand and robots.allowed(cand):
                targets.append(cand)
            if len(targets) >= self.max_pages - 1:
                break
        disallowed = [u for u in url_pool if not robots.allowed(u)]
        if disallowed:
            b.limit(f"{len(disallowed)} URL(s) disallowed by robots.txt were not fetched.")
        for url in targets:
            try:
                resp = await self.http.get(url)
                b.retry_count += resp.retries
            except (FetchError, UnsafeTarget) as e:
                b.error(e.code, f"could not fetch {urlsplit(url).path}: {e.message}")
                continue
            if resp.ok and resp.content:
                fetched.append(parse_html(resp.text, resp.final_url))
        self._profile(b, fetched, site_domain, home_resp.content.decode("utf-8", "replace"))
        request.context["social_profiles"] = b.observations[-1].items[0].get("social_profiles", {}) if b.observations else {}
        return b.build()

    # ---------------------------------------------------------------- helpers
    async def _sitemap_entries(self, domain: str, robots: Robots) -> tuple[list[SitemapEntry], str | None, str | None]:
        queue = [u for u in robots.sitemaps if same_site(u, domain)][: self.max_sitemap_files] or [
            f"https://{domain}/sitemap.xml",
            f"https://{domain}/sitemap_index.xml",
        ]
        seen_files: set[str] = set()
        entries: dict[str, SitemapEntry] = {}
        first_ok: str | None = None
        last_err: str | None = None
        while queue and len(seen_files) < self.max_sitemap_files and len(entries) < self.max_sitemap_urls:
            url = queue.pop(0)
            if url in seen_files:
                continue
            seen_files.add(url)
            try:
                resp = await self.http.get(url)
            except (FetchError, UnsafeTarget) as e:
                last_err = e.code.value
                continue
            if not resp.ok:
                continue
            try:
                urls, children = parse_sitemap(resp.content)
            except ValueError as e:
                last_err = f"{ErrorCode.MALFORMED_RESPONSE.value}: {str(e)[:80]}"
                continue
            first_ok = first_ok or resp.final_url
            for c in children:
                if same_site(c, domain) and c not in seen_files:
                    queue.append(c)
            for entry in urls:
                if same_site(entry.url, domain) and entry.url not in entries:
                    entries[entry.url] = entry
                    if len(entries) >= self.max_sitemap_urls:
                        break
            if entries and not children:
                # good enough when a flat sitemap was found; avoid probing the index fallback
                queue = [q for q in queue if q not in (f"https://{domain}/sitemap_index.xml",)]
        return list(entries.values()), first_ok, last_err

    def _profile(self, b: ResultBuilder, pages: list[ParsedPage], domain: str, home_html: str) -> None:
        home = pages[0]
        org = jsonld_org(home)
        field_ev: dict[str, list[str]] = {}

        def ev(field: str, claim: str, url: str, conf: float, raw: dict, metric: str | None = None, value=None) -> None:
            eid = b.add_evidence(
                observation_type=ObservationType.WEBSITE_COMPANY_PROFILE,
                claim=claim,
                source_ref=f"{field}:{url}",
                source_url=url,
                confidence=conf,
                raw=raw,
                metric=metric,
                value=value,
            )
            field_ev.setdefault(field, []).append(eid)

        description = (org.get("description") if isinstance(org.get("description"), str) else None) or home.description
        if description:
            ev("description", f'Site describes itself as: "{description[:200]}"', home.url, 0.9, {"description": description})

        emails: list[str] = []
        phones: list[str] = []
        socials: dict[str, str] = {}
        locations: list[str] = []
        ctas: list[str] = []
        offers: list[str] = []
        prices: list[str] = []
        services, products = [], []
        for p in pages:
            kind = classify_url(p.url)
            for e in extract_emails(p, domain):
                if e not in emails:
                    emails.append(e)
            for ph in extract_phones(p):
                if ph not in phones:
                    phones.append(ph)
            for k, v in extract_socials(p).items():
                socials.setdefault(k, v)
            locations += [x for x in jsonld_locations(p) if x not in locations]
            ctas += [c for c in extract_ctas(p) if c not in ctas]
            offers += [o for o in extract_offers(p.text) if o not in offers]
            if kind is PageKind.PRICING:
                prices += [x for x in extract_prices(p.text) if x not in prices]
            if kind is PageKind.SERVICE:
                services += [h for h in p.headings[1:12] if h not in services]
            if kind is PageKind.PRODUCT:
                products += [h for h in p.headings[1:12] if h not in products]

        if emails:
            ev("emails", f"Public contact email(s) published: {', '.join(emails[:3])}", pages[0].url, 0.9, {"emails": emails})
        if phones:
            ev("phones", f"Public phone number(s) published: {', '.join(phones[:3])}", pages[0].url, 0.9, {"phones": phones})
        if socials:
            ev(
                "social_profiles",
                f"Linked social profiles: {', '.join(sorted(socials))}",
                pages[0].url,
                0.9,
                {"profiles": socials},
            )
        techs = detect_technologies(home_html)
        if techs:
            ev(
                "technologies",
                f"Technology signatures in homepage HTML: {', '.join(techs)}",
                home.url,
                0.85,
                {"technologies": techs},
            )
        if locations:
            ev("locations", f"Structured-data address(es): {'; '.join(locations[:3])}", home.url, 0.88, {"locations": locations})
        if ctas:
            ev("ctas", f"Calls-to-action observed: {', '.join(ctas[:6])}", home.url, 0.85, {"ctas": ctas})
        if offers:
            ev("offers", f'Offer language observed: "{offers[0][:150]}"', home.url, 0.8, {"offers": offers})
        pricing_page = next((p for p in pages if classify_url(p.url) is PageKind.PRICING), None)
        if prices and pricing_page:
            ev(
                "prices",
                f"{len(prices)} price point(s) published on {pricing_page.url}",
                pricing_page.url,
                0.88,
                {"prices": prices},
                metric="published_price_points",
                value=len(prices),
            )
        if services:
            src = next(p.url for p in pages if classify_url(p.url) is PageKind.SERVICE)
            ev("services", f"Service headings on {src}: {', '.join(services[:5])}", src, 0.8, {"services": services})
        if products:
            src = next(p.url for p in pages if classify_url(p.url) is PageKind.PRODUCT)
            ev("products", f"Product headings on {src}: {', '.join(products[:5])}", src, 0.8, {"products": products})

        models = infer_business_model(pages, techs, request_terms=None)
        for label, indicators in models.items():
            eid = b.add_evidence(
                observation_type=ObservationType.WEBSITE_COMPANY_PROFILE,
                claim=f"Business model appears to include '{label}' (indicators: {', '.join(indicators[:4])})",
                source_ref=f"business_model:{label}",
                source_url=home.url,
                source_type="rule_classification",
                claim_kind=ClaimKind.INFERENCE,
                confidence=min(0.85, 0.45 + 0.1 * len(indicators)),
                raw={"label": label, "indicators": indicators},
            )
            field_ev.setdefault("business_model", []).append(eid)

        profile = {
            "title": home.title or None,
            "description": description,
            "services": services[:20],
            "products": products[:20],
            "emails": emails,
            "phones": phones,
            "social_profiles": socials,
            "technologies": techs,
            "locations": locations,
            "ctas": ctas,
            "offers": offers,
            "prices": prices,
            "business_model": sorted(models),
            "business_model_indicators": models,
            "pages_read": [p.url for p in pages],
            "field_evidence": field_ev,
        }
        b.add_observation(
            ObservationType.WEBSITE_COMPANY_PROFILE,
            metrics={
                "pages_read": len(pages),
                "email_count": len(emails),
                "phone_count": len(phones),
                "social_profile_count": len(socials),
                "cta_count": len(ctas),
                "offer_mentions": len(offers),
                "published_price_points": len(prices),
                "location_count": len(locations),
            },
            items=[profile],
            evidence_refs=[e for refs in field_ev.values() for e in refs],
            claim_kind=ClaimKind.FACT,
        )


_BM_RULES: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "ecommerce": [
        ("cart/checkout", re.compile(r"add to (cart|bag|basket)|/cart\b|checkout", re.I)),
        ("shop section", re.compile(r"\bshop (now|all)\b|/shop\b|/collections/", re.I)),
        ("free shipping", re.compile(r"free (shipping|delivery)", re.I)),
    ],
    "saas_subscription": [
        ("free trial", re.compile(r"free trial|start (your )?trial", re.I)),
        ("per-period pricing", re.compile(r"(/|per )\s?(month|mo|year|user|seat)\b", re.I)),
        ("sign-up / log-in", re.compile(r"\b(sign up|log ?in|create (an )?account)\b", re.I)),
        ("demo request", re.compile(r"(request|book|get) a demo", re.I)),
    ],
    "local_service": [
        ("appointment/booking", re.compile(r"\b(book (an )?appointment|book now|reserve|schedule a visit)\b", re.I)),
        ("opening hours", re.compile(r"\b(opening hours|open (daily|mon|monday)|hours:)", re.I)),
        ("call to visit", re.compile(r"\b(visit us|our (shop|store|bakery|clinic|salon)|directions)\b", re.I)),
    ],
    "b2b_services": [
        ("quote request", re.compile(r"(request|get) a (free )?quote|proposal", re.I)),
        ("case studies", re.compile(r"case stud(y|ies)|our clients|trusted by", re.I)),
        ("corporate offering", re.compile(r"\b(corporate|enterprise|b2b|wholesale)\b", re.I)),
    ],
    "food_service": [
        ("menu", re.compile(r"\bmenu\b", re.I)),
        ("order online", re.compile(r"order (online|now)|delivery|takeaway|catering", re.I)),
    ],
    "marketplace": [
        ("sellers/vendors", re.compile(r"become a (seller|vendor|partner)|list your|for (sellers|vendors)", re.I)),
    ],
}
_JSONLD_BM = {
    "LocalBusiness": "local_service",
    "Bakery": "food_service",
    "Restaurant": "food_service",
    "Store": "ecommerce",
    "SoftwareApplication": "saas_subscription",
    "Product": "ecommerce",
}
_TECH_BM = {"Shopify": "ecommerce", "WooCommerce": "ecommerce", "Stripe": "saas_subscription", "Calendly": "local_service"}


def infer_business_model(
    pages: list[ParsedPage], techs: list[str], request_terms: list[str] | None = None
) -> dict[str, list[str]]:
    """Deterministic business-model labels (INFERENCE). A label needs >=2 independent indicators."""
    text = " ".join([p.text[:8000] for p in pages] + [lk.url + " " + lk.text for p in pages for lk in p.links[:300]])
    found: dict[str, list[str]] = {}
    for label, rules in _BM_RULES.items():
        hits = [name for name, pat in rules if pat.search(text)]
        if hits:
            found[label] = hits
    for p in pages:
        for obj in p.jsonld:
            typ = obj.get("@type")
            for schema_type in typ if isinstance(typ, list) else [typ]:
                if isinstance(schema_type, str):
                    bm_label = _JSONLD_BM.get(schema_type) or ("local_service" if schema_type.endswith("Business") else None)
                    if bm_label:
                        found.setdefault(bm_label, []).append(f"schema.org {schema_type}")
    for tech in techs:
        if tech in _TECH_BM:
            found.setdefault(_TECH_BM[tech], []).append(f"{tech} detected")
    return {k: sorted(set(v)) for k, v in found.items() if len(set(v)) >= 2}
