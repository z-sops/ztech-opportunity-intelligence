"""Deterministic extraction helpers: HTML, robots.txt, sitemaps, RSS/Atom feeds.

These are the 'DETERMINISTIC EXTRACTION' stage of spec #25. No AI here.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import xml.etree.ElementTree as ET  # only for ParseError type
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..domain.identity import iso, parse_iso
from ..domain.taxonomy import PageKind

MAX_TEXT = 20_000
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}\b")
PRICE_RE = re.compile(
    r"(?:(?:US)?\$|€|£|Rs\.?|PKR|AED|₹)\s?\d[\d,]*(?:\.\d{1,2})?(?:\s?(?:/|per)\s?(?:month|mo|year|yr|user|seat|kg|person)\b)?",
    re.IGNORECASE,
)
OFFER_RE = re.compile(
    r"(\d{1,2}\s?%\s?off|free shipping|free delivery|free trial|discount|limited[- ]time|special offer|"
    r"promo(?:tion)? code|buy one get one|bogo|sale ends|flash sale|seasonal offer)",
    re.IGNORECASE,
)
CTA_RE = re.compile(
    r"^(book( a| your)?|get (started|a quote|a demo|in touch)|request( a)? (quote|demo|call)|contact us|"
    r"order( now| online)?|buy( now)?|shop( now)?|start( free)?( trial)?|sign up|try (it )?free|schedule|"
    r"call( us)?( now)?|reserve|subscribe|download|enquire|inquire|apply now|join)\b",
    re.IGNORECASE,
)
SOCIAL_HOSTS = {
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "linkedin.com": "linkedin",
    "twitter.com": "x",
    "x.com": "x",
    "youtube.com": "youtube",
    "tiktok.com": "tiktok",
    "pinterest.com": "pinterest",
}
TECH_SIGNATURES: list[tuple[str, re.Pattern[str]]] = [
    ("WordPress", re.compile(r"wp-content/|wp-includes/", re.I)),
    ("Shopify", re.compile(r"cdn\.shopify\.com|Shopify\.theme", re.I)),
    ("Wix", re.compile(r"static\.wixstatic\.com|wix-code", re.I)),
    ("Squarespace", re.compile(r"static1\.squarespace\.com", re.I)),
    ("Webflow", re.compile(r"assets\.website-files\.com|webflow\.js", re.I)),
    ("Next.js", re.compile(r"__NEXT_DATA__|/_next/static/", re.I)),
    ("HubSpot", re.compile(r"js\.hs-scripts\.com|js\.hsforms\.net", re.I)),
    ("Google Tag Manager", re.compile(r"googletagmanager\.com/gtm\.js", re.I)),
    ("Google Analytics", re.compile(r"google-analytics\.com|gtag\(\s*['\"]config['\"]", re.I)),
    ("Meta Pixel", re.compile(r"connect\.facebook\.net/[^\"']*/fbevents\.js", re.I)),
    ("Google Ads Tag", re.compile(r"googleadservices\.com|gtag\(\s*['\"]config['\"]\s*,\s*['\"]AW-", re.I)),
    ("LinkedIn Insight Tag", re.compile(r"snap\.licdn\.com/li\.lms-analytics", re.I)),
    ("Intercom", re.compile(r"widget\.intercom\.io", re.I)),
    ("Stripe", re.compile(r"js\.stripe\.com", re.I)),
    ("WooCommerce", re.compile(r"woocommerce", re.I)),
    ("Calendly", re.compile(r"assets\.calendly\.com|calendly\.com/", re.I)),
]

_KIND_PATTERNS: list[tuple[PageKind, re.Pattern[str]]] = [
    (PageKind.PRICING, re.compile(r"/(pricing|prices?|plans?|packages?|rates?|menu)(/|$|\.)", re.I)),
    (PageKind.CAREERS, re.compile(r"/(careers?|jobs?|hiring|join-?us|work-?with-?us|vacancies)(/|$|\.)", re.I)),
    (
        PageKind.CASE_STUDY,
        re.compile(r"/(case-?stud(y|ies)|customers?|success-?stories|portfolio|testimonials?|work)(/|$|\.)", re.I),
    ),
    (PageKind.BLOG, re.compile(r"/(blog|news|articles?|insights|guides?|resources|stories|posts?|journal)(/|$|\.)", re.I)),
    (PageKind.CONTACT, re.compile(r"/(contact(-?us)?|get-?in-?touch|book|appointment|quote)(/|$|\.)", re.I)),
    (PageKind.ABOUT, re.compile(r"/(about(-?us)?|company|team|our-?story|who-?we-?are)(/|$|\.)", re.I)),
    (PageKind.LOCATION, re.compile(r"/(locations?|stores?|branches|find-?us|areas?-?we-?serve)(/|$|\.)", re.I)),
    (PageKind.LANDING, re.compile(r"/(lp|landing|offers?|promo(tions?)?|campaigns?|deals?|special)(/|$|\.|-)", re.I)),
    (PageKind.SERVICE, re.compile(r"/(services?|solutions?|what-?we-?do|catering|treatments?)(/|$|\.)", re.I)),
    (PageKind.PRODUCT, re.compile(r"/(products?|shop|store|collections?|catalog(ue)?|features?|items?)(/|$|\.)", re.I)),
    (PageKind.LEGAL, re.compile(r"/(privacy|terms|cookies?|legal|gdpr|refund|imprint)(/|$|\.|-)", re.I)),
]


def classify_url(url: str) -> PageKind:
    try:
        path = urlsplit(url).path or "/"
    except ValueError:
        return PageKind.OTHER
    if path in ("", "/"):
        return PageKind.HOMEPAGE
    for kind, pat in _KIND_PATTERNS:
        if pat.search(path.lower() + ("/" if not path.endswith("/") else "")):
            return kind
    return PageKind.OTHER


def is_article_url(url: str) -> bool:
    """A blog-section URL that looks like an individual post (not the index or a tag page)."""
    try:
        path = urlsplit(url).path.rstrip("/")
    except ValueError:
        return False
    if classify_url(url) is not PageKind.BLOG:
        return False
    segs = [s for s in path.split("/") if s]
    if len(segs) < 2:
        return False
    if any(s in {"tag", "tags", "category", "categories", "author", "page", "feed"} for s in segs):
        return False
    return len(segs[-1]) >= 4


# ------------------------------------------------------------------- HTML
@dataclass
class Link:
    url: str
    text: str


@dataclass
class ParsedPage:
    url: str
    title: str = ""
    description: str = ""
    site_name: str = ""
    text: str = ""
    links: list[Link] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)
    jsonld: list[dict] = field(default_factory=list)
    published_at: str | None = None
    raw_html: str = ""

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def parse_html(html: str, base_url: str) -> ParsedPage:
    tree = HTMLParser(html or "")
    page = ParsedPage(url=base_url, raw_html=html[:500_000])
    t = tree.css_first("title")
    page.title = _clean(t.text()) if t else ""
    for m in tree.css("meta"):
        name = (m.attributes.get("name") or m.attributes.get("property") or "").lower()
        content = _clean(m.attributes.get("content") or "")
        if name in ("description", "og:description") and not page.description:
            page.description = content[:500]
        elif name == "og:site_name":
            page.site_name = content[:120]
        elif name in ("article:published_time", "og:published_time", "datepublished", "date") and not page.published_at:
            dt = parse_iso(content)
            page.published_at = iso(dt) if dt else None
    for lk in tree.css("link[rel]"):
        rel = (lk.attributes.get("rel") or "").lower()
        typ = (lk.attributes.get("type") or "").lower()
        href = lk.attributes.get("href")
        if "alternate" in rel and ("rss" in typ or "atom" in typ) and href:
            page.feeds.append(urljoin(base_url, href))
    for s in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.text() or "")
        except ValueError:
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict):
                graph = obj.get("@graph")
                page.jsonld.extend(g for g in (graph if isinstance(graph, list) else [obj]) if isinstance(g, dict))
    if not page.published_at:
        for obj in page.jsonld:
            dt = parse_iso(str(obj.get("datePublished") or ""))
            if dt:
                page.published_at = iso(dt)
                break
    if not page.published_at:
        tm = tree.css_first("article time[datetime], time[datetime]")
        if tm:
            dt = parse_iso(tm.attributes.get("datetime") or "")
            page.published_at = iso(dt) if dt else None
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "data:")):
            continue
        page.links.append(Link(url=urljoin(base_url, href), text=_clean(a.text())[:120]))
    for h in tree.css("h1, h2, h3"):
        txt = _clean(h.text())
        if 2 < len(txt) < 120:
            page.headings.append(txt)
    for node in tree.css("script, style, noscript, svg, template"):
        node.decompose()
    body = tree.body
    page.text = _clean(body.text(separator=" ") if body else "")[:MAX_TEXT]
    return page


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def same_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


def extract_emails(page: ParsedPage, domain: str | None) -> list[str]:
    found: set[str] = set()
    for lk in page.links:
        if lk.url.lower().startswith("mailto:"):
            found.add(lk.url[7:].split("?")[0].strip().lower())
    for m in EMAIL_RE.findall(page.text):
        found.add(m.lower())
    out = [e for e in found if not e.endswith((".png", ".jpg", ".webp", ".gif", ".svg")) and "example." not in e]
    if domain:
        out.sort(key=lambda e: (not e.endswith(domain), e))
    return out[:5]


def extract_phones(page: ParsedPage) -> list[str]:
    phones = []
    for lk in page.links:
        if lk.url.lower().startswith("tel:"):
            num = re.sub(r"[^\d+]", "", lk.url[4:])
            if 7 <= len(num.lstrip("+")) <= 15:
                phones.append(num)
    for obj in page.jsonld:
        tel = obj.get("telephone")
        if isinstance(tel, str):
            num = re.sub(r"[^\d+]", "", tel)
            if 7 <= len(num.lstrip("+")) <= 15:
                phones.append(num)
    return list(dict.fromkeys(phones))[:5]


def extract_socials(page: ParsedPage) -> dict[str, str]:
    out: dict[str, str] = {}
    candidates = [lk.url for lk in page.links]
    for obj in page.jsonld:
        same = obj.get("sameAs")
        if isinstance(same, list):
            candidates += [s for s in same if isinstance(s, str)]
    for url in candidates:
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")
        platform = SOCIAL_HOSTS.get(host)
        if platform and platform not in out:
            path = urlsplit(url).path.strip("/")
            if path and not path.startswith(("share", "sharer", "intent", "dialog")):
                out[platform] = url.split("?")[0]
    return out


def twitter_handle(url: str | None) -> str | None:
    if not url:
        return None
    seg = urlsplit(url).path.strip("/").split("/")[0]
    if re.fullmatch(r"[A-Za-z0-9_]{1,15}", seg or "") and seg.lower() not in {"home", "share", "intent", "i"}:
        return seg
    return None


def detect_technologies(html: str) -> list[str]:
    return [name for name, pat in TECH_SIGNATURES if pat.search(html or "")]


def extract_ctas(page: ParsedPage) -> list[str]:
    seen: dict[str, None] = {}
    for lk in page.links:
        if lk.text and len(lk.text) <= 40 and CTA_RE.match(lk.text):
            seen.setdefault(lk.text, None)
    return list(seen)[:12]


def extract_offers(text: str) -> list[str]:
    out: dict[str, None] = {}
    for m in OFFER_RE.finditer(text or ""):
        start = max(0, m.start() - 60)
        out.setdefault(_clean(text[start : m.end() + 60]), None)
    return list(out)[:8]


def extract_prices(text: str) -> list[str]:
    return list(dict.fromkeys(_clean(p) for p in PRICE_RE.findall(text or "")))[:15]


def jsonld_org(page: ParsedPage) -> dict:
    for obj in page.jsonld:
        typ = obj.get("@type")
        types = typ if isinstance(typ, list) else [typ]
        if any(
            isinstance(t, str)
            and (
                t in ("Organization", "Corporation", "LocalBusiness")
                or t.endswith("Business")
                or t in ("Bakery", "Restaurant", "Store")
            )
            for t in types
        ):
            return obj
    return {}


def jsonld_locations(page: ParsedPage) -> list[str]:
    locs: list[str] = []
    for obj in page.jsonld:
        addr = obj.get("address")
        addrs = addr if isinstance(addr, list) else [addr]
        for a in addrs:
            if isinstance(a, dict):
                parts = [a.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
                s = ", ".join(str(p) for p in parts if isinstance(p, str) and p)
                if s:
                    locs.append(s)
    return list(dict.fromkeys(locs))[:10]


# ------------------------------------------------------------- robots.txt
@dataclass
class Robots:
    disallow: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)

    def allowed(self, url: str) -> bool:
        path = urlsplit(url).path or "/"
        best_allow = max((len(a) for a in self.allow if path.startswith(a)), default=-1)
        best_dis = max((len(d) for d in self.disallow if d and path.startswith(d)), default=-1)
        return best_allow >= best_dis


def parse_robots(text: str, ua_token: str = "ztechopportunityintelligence") -> Robots:  # noqa: S107
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    robots = Robots()
    last_was_agent = False
    for raw in (text or "").splitlines()[:5000]:
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, val = (s.strip() for s in line.split(":", 1))
        key = key.lower()
        if key == "sitemap":
            robots.sitemaps.append(val)
            continue
        if key == "user-agent":
            if not last_was_agent and (agents or rules):
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(val.lower())
            last_was_agent = True
        elif key in ("disallow", "allow"):
            rules.append((key, val))
            last_was_agent = False
    if agents or rules:
        groups.append((agents, rules))
    chosen = next((r for a, r in groups if any(ua_token in x for x in a)), None)
    if chosen is None:
        chosen = next((r for a, r in groups if "*" in a), [])
    for k, v in chosen:
        (robots.disallow if k == "disallow" else robots.allow).append(v)
    return robots


# ---------------------------------------------------------------- sitemaps
@dataclass
class SitemapEntry:
    url: str
    lastmod: str | None = None


# A plain DOCTYPE (for example the PUBLIC sitemap DTD many CMSs still emit) is harmless:
# expat never fetches it. What makes XML dangerous is an entity declaration, an internal
# DTD subset (where entities are declared) or an external entity reference. Those are
# refused twice: by this cheap prolog check and by defusedxml itself.
_INTERNAL_SUBSET_RE = re.compile(rb"<!DOCTYPE[^>]*\[")
_PROLOG_SCAN = 8192


def _refuse_unsafe_xml(data: bytes, what: str) -> None:
    head = data[:_PROLOG_SCAN]
    if b"<!ENTITY" in head.upper() or _INTERNAL_SUBSET_RE.search(head):
        raise ValueError(f"entities and internal DTD subsets are not allowed in {what}")


def _safe_parse(data: bytes):
    return safe_fromstring(data, forbid_dtd=False, forbid_entities=True, forbid_external=True)


def maybe_gunzip(content: bytes, max_bytes: int = 10_000_000) -> bytes:
    if content[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as gz:
            data = gz.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("decompressed sitemap too large")
        return data
    return content


def parse_sitemap(content: bytes) -> tuple[list[SitemapEntry], list[str]]:
    """Return (url entries, child sitemap urls). Raises ValueError on malformed XML."""
    data = maybe_gunzip(content)
    _refuse_unsafe_xml(data, "sitemaps")
    try:
        root = _safe_parse(data)
    except (ET.ParseError, DefusedXmlException) as e:
        raise ValueError(f"malformed or unsafe sitemap XML: {e}") from e
    tag = root.tag.split("}")[-1]
    entries: list[SitemapEntry] = []
    children: list[str] = []
    for node in root:
        loc = lastmod = None
        for child in node:
            ctag = child.tag.split("}")[-1]
            if ctag == "loc" and child.text:
                loc = child.text.strip()
            elif ctag == "lastmod" and child.text:
                dt = parse_iso(child.text.strip())
                lastmod = iso(dt) if dt else None
        if not loc:
            continue
        if tag == "sitemapindex":
            children.append(loc)
        else:
            entries.append(SitemapEntry(url=loc, lastmod=lastmod))
    return entries, children


# ------------------------------------------------------------------ feeds
@dataclass
class FeedItem:
    url: str
    title: str
    published_at: str | None


def parse_feed(content: bytes) -> list[FeedItem]:
    _refuse_unsafe_xml(content, "feeds")
    try:
        root = _safe_parse(content)
    except (ET.ParseError, DefusedXmlException) as e:
        raise ValueError(f"malformed or unsafe feed XML: {e}") from e
    items: list[FeedItem] = []
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        link = title = date = None
        for child in node:
            ctag = child.tag.split("}")[-1]
            if ctag == "title":
                title = (child.text or "").strip()
            elif ctag == "link":
                link = (child.text or "").strip() or child.attrib.get("href")
            elif ctag in ("pubDate", "published", "updated", "date") and child.text and not date:
                date = child.text.strip()
        if not link:
            continue
        published = None
        if date:
            dt = parse_iso(date)
            if dt is None:
                try:
                    dt = parsedate_to_datetime(date)
                except (TypeError, ValueError):
                    dt = None
            published = iso(dt) if dt else None
        items.append(FeedItem(url=link, title=(title or "")[:200], published_at=published))
    return items[:200]


# -------------------------------------------------------------- keywords
_STOP = set(
    "a an the and or of to in on for with your you our we is are be how what why when best top new guide tips "
    "from by at as it this that vs via into about more most can will do does using use ideas ways need know "
    "2023 2024 2025 2026 2027".split()
)


def keywords(text: str) -> list[str]:
    toks = re.findall(r"[a-z][a-z0-9-]{2,}", (text or "").lower())
    return [t for t in toks if t not in _STOP]


# ------------------------------------------------------------------ topics
def _stem(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 4 and tok.endswith(("ches", "shes", "sses", "xes", "zes")):
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def topic_tokens(phrase: str) -> set[str]:
    return {_stem(t) for t in keywords(phrase)}


def topic_matches(text: str, phrases: list[str]) -> list[str]:
    """Return the caller phrases (e.g. products/services) that a text is about.

    Deterministic: a phrase matches when all of its stemmed keywords appear (phrases of up to
    two keywords) or at least two thirds of them appear (longer phrases).
    """
    have = topic_tokens(text)
    out = []
    for ph in phrases:
        need = topic_tokens(ph)
        if not need:
            continue
        hit = len(need & have)
        if (len(need) <= 2 and hit == len(need)) or (len(need) > 2 and hit / len(need) >= 2 / 3):
            out.append(ph)
    return out
