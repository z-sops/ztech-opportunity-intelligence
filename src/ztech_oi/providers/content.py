"""Content Intelligence Provider (spec #16) — REAL, no API key needed.

Builds an article inventory with publication dates, then counts activity in
30/60/90-day windows. Date sources, in order of trust:
  1. RSS/Atom feed  <pubDate>/<published>       (fact, 0.92)
  2. article page   article:published_time / JSON-LD datePublished / <time> (fact, 0.88)
  3. sitemap        <lastmod>                     (reported separately as a
                    *modification* proxy — never counted as a publication)
Topic themes are extracted deterministically from titles; if an LLM is
configured it may additionally label themes, stored as INFERENCE.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from urllib.parse import urljoin, urlsplit

from ..domain.errors import FetchError, UnsafeTarget
from ..domain.identity import iso, parse_iso, utcnow
from ..domain.models import ProviderResult
from ..domain.taxonomy import ClaimKind, ErrorCode, ObservationType, ProviderStatus
from ..integrations.llm import LLM_CONFIDENCE_CAP, LLMClient, NullLLM
from ..net.http import SafeHttpClient
from ..net.parsing import is_article_url, keywords, parse_feed, parse_html, same_site, topic_matches
from .base import ResearchRequest, ResultBuilder, simple_result

WINDOWS = (30, 60, 90)
FEED_FALLBACK_PATHS = ("/feed", "/rss.xml", "/feed.xml", "/blog/feed", "/blog/rss.xml", "/atom.xml")


class ContentProvider:
    name = "content"
    source_type = "website"
    scope = "per_entity"

    def __init__(self, http: SafeHttpClient, llm: LLMClient | None = None, *, max_article_fetches: int = 6) -> None:
        self.http = http
        self.llm = llm or NullLLM()
        self.max_article_fetches = max_article_fetches

    async def collect(self, request: ResearchRequest) -> ProviderResult:
        domain = request.context.get("site_domain") or request.entity.domain
        if not domain:
            return simple_result(
                self.name,
                self.source_type,
                request,
                ProviderStatus.UNAVAILABLE,
                ErrorCode.INSUFFICIENT_EVIDENCE,
                "entity has no domain",
            )
        if request.context.get("site_reachable") is not True:
            # Content is only observable on a site the website provider actually reached. Never
            # report "0 articles" for a site that was never seen (absence of evidence != zero).
            reason = (
                "website unreachable; content cannot be observed"
                if request.context.get("site_reachable") is False
                else "website provider did not confirm the site is reachable (not run or failed)"
            )
            return simple_result(
                self.name, self.source_type, request, ProviderStatus.UNAVAILABLE, ErrorCode.SOURCE_UNAVAILABLE, reason
            )
        b = ResultBuilder(self.name, self.source_type, request)
        now = utcnow()
        articles: dict[str, dict] = {}

        # 1) feeds
        feed_urls = [u for u in request.context.get("feeds", []) if same_site(u, domain)][:2]
        if not feed_urls:
            feed_urls = [f"https://{domain}{p}" for p in FEED_FALLBACK_PATHS[:3]]
        feed_used = None
        for fu in feed_urls:
            try:
                resp = await self.http.get(fu)
            except (FetchError, UnsafeTarget):
                continue
            if not resp.ok or b"<" not in resp.content[:200]:
                continue
            try:
                items = parse_feed(resp.content)
            except ValueError:
                b.error(ErrorCode.MALFORMED_RESPONSE, f"feed at {urlsplit(fu).path} is malformed")
                continue
            if items:
                feed_used = resp.final_url
                for it in items:
                    url = urljoin(resp.final_url, it.url)
                    if same_site(url, domain):
                        articles.setdefault(
                            url,
                            {
                                "url": url,
                                "title": it.title,
                                "published_at": it.published_at,
                                "date_source": "feed" if it.published_at else None,
                                "lastmod": None,
                            },
                        )
                break

        # 2) sitemap article candidates
        sm = [e for e in request.context.get("sitemap_entries", []) if is_article_url(e["url"])]
        for e in sm:
            a = articles.setdefault(
                e["url"], {"url": e["url"], "title": "", "published_at": None, "date_source": None, "lastmod": None}
            )
            a["lastmod"] = e.get("lastmod")

        if not articles:
            b.limit("No feed and no article-shaped URLs found; publishing activity could not be observed.")
            ev = b.add_evidence(
                observation_type=ObservationType.CONTENT_INVENTORY,
                claim=f"No articles were observable on {domain} (no feed, no blog URLs in sitemap/homepage links)",
                source_ref=f"no_articles:{domain}",
                source_url=f"https://{domain}/",
                confidence=0.6,
                metric="article_count_observed",
                value=0,
            )
            # Zero is reported, but with low confidence and an explicit flag:
            # absence of evidence is not evidence of absence.
            b.add_observation(
                ObservationType.CONTENT_INVENTORY,
                metrics={
                    "article_count_observed": 0,
                    "dated_article_count": 0,
                    "articles_last_30d": None,
                    "articles_last_60d": None,
                    "articles_last_90d": None,
                    "articles_modified_last_60d": None,
                },
                evidence_refs=[ev],
            )
            return b.build(ProviderStatus.PARTIAL)

        # 3) fetch a bounded sample of undated articles for page-level dates
        undated = sorted(
            (a for a in articles.values() if not a["published_at"]), key=lambda a: a.get("lastmod") or "", reverse=True
        )[: self.max_article_fetches]
        for a in undated:
            try:
                resp = await self.http.get(a["url"])
            except (FetchError, UnsafeTarget) as e:
                b.error(e.code, f"article fetch failed: {urlsplit(a['url']).path}")
                continue
            if resp.ok:
                page = parse_html(resp.text, resp.final_url)
                a["title"] = a["title"] or page.title
                if page.published_at:
                    a["published_at"], a["date_source"] = page.published_at, "page_meta"

        # 4) evidence + window counts
        refs: list[str] = []
        topic_tokens: Counter[str] = Counter()
        relevant_terms = list(request.products_services)
        relevant_60 = 0
        counts = dict.fromkeys(WINDOWS, 0)
        modified_60 = 0
        dated = 0
        for a in articles.values():
            pub = parse_iso(a["published_at"])
            if pub:
                dated += 1
                age = now - pub
                for w in WINDOWS:
                    if timedelta(0) <= age <= timedelta(days=w):
                        counts[w] += 1
                conf = 0.92 if a["date_source"] == "feed" else 0.88
                refs.append(
                    b.add_evidence(
                        observation_type=ObservationType.CONTENT_INVENTORY,
                        claim=f'Article "{(a["title"] or a["url"])[:120]}" published {a["published_at"][:10]} (source: {a["date_source"]})',
                        source_ref=a["url"],
                        source_url=a["url"],
                        confidence=conf,
                        observed_at=a["published_at"],
                        raw={"title": a["title"], "date_source": a["date_source"]},
                    )
                )
            lm = parse_iso(a.get("lastmod"))
            if lm and timedelta(0) <= now - lm <= timedelta(days=60):
                modified_60 += 1
            toks = keywords(a["title"] or urlsplit(a["url"]).path.replace("-", " "))
            topic_tokens.update(set(toks))
            a_topics = (
                topic_matches(a["title"] or urlsplit(a["url"]).path.replace("-", " "), relevant_terms) if relevant_terms else []
            )
            a["topics"] = a_topics
            if pub and now - pub <= timedelta(days=60) and a_topics:
                relevant_60 += 1

        summary_ev = b.add_evidence(
            observation_type=ObservationType.CONTENT_INVENTORY,
            claim=(
                f"{len(articles)} article(s) observed on {domain}; {dated} with a publication date; "
                f"{counts[30]} / {counts[60]} / {counts[90]} published in the last 30 / 60 / 90 days"
            ),
            source_ref=f"content_summary:{domain}",
            source_url=feed_used or f"https://{domain}/",
            confidence=0.9 if feed_used else 0.8,
            metric="articles_last_60d",
            value=counts[60] if dated else None,
            raw={"feed": feed_used, "dated": dated, "windows": {str(k): v for k, v in counts.items()}},
        )
        if dated == 0:
            b.limit("Articles were found but none carried a publication date; window counts are unknown (null), not zero.")
        elif dated < len(articles):
            b.limit(
                f"{len(articles) - dated} of {len(articles)} articles had no observable publication date; window counts cover dated articles only."
            )
        if not feed_used:
            b.limit("No RSS/Atom feed found; publication dates came from a bounded sample of article pages.")

        top_topics = [t for t, _ in topic_tokens.most_common(10)]
        b.add_observation(
            ObservationType.CONTENT_INVENTORY,
            metrics={
                "article_count_observed": len(articles),
                "dated_article_count": dated,
                "articles_last_30d": counts[30] if dated else None,
                "articles_last_60d": counts[60] if dated else None,
                "articles_last_90d": counts[90] if dated else None,
                "relevant_articles_last_60d": relevant_60 if (dated and relevant_terms) else None,
                "articles_modified_last_60d": modified_60,
                "from_feed": 1 if feed_used else 0,
            },
            items=sorted(articles.values(), key=lambda a: a["published_at"] or "", reverse=True)[:150],
            evidence_refs=[summary_ev, *refs[:100]],
        )

        # 5) topics: deterministic keywords (fact = observed words) + optional LLM themes (inference)
        topic_items = [{"topic": t, "article_count": topic_tokens[t], "method": "title_keywords"} for t in top_topics]
        topic_refs: list[str] = []
        if self.llm.configured and len(articles) >= 3:
            themes = await self._llm_themes(request.entity.company_name, list(articles.values())[:40])
            for th in themes:
                topic_refs.append(
                    b.add_evidence(
                        observation_type=ObservationType.CONTENT_TOPICS,
                        claim=f'LLM-labelled content theme "{th["theme"]}" across {th["article_count"]} observed title(s)',
                        source_ref=f"theme:{th['theme']}",
                        source_type="llm_interpretation",
                        claim_kind=ClaimKind.INFERENCE,
                        confidence=th["confidence"],
                        raw={"theme": th["theme"], "article_count": th["article_count"]},
                    )
                )
                topic_items.append({"topic": th["theme"], "article_count": th["article_count"], "method": "llm_label"})
        if topic_items:
            b.add_observation(
                ObservationType.CONTENT_TOPICS,
                items=topic_items,
                evidence_refs=topic_refs or [summary_ev],
                claim_kind=ClaimKind.INFERENCE if topic_refs else ClaimKind.FACT,
            )
        return b.build()

    async def _llm_themes(self, company: str, articles: list[dict]) -> list[dict]:
        titles = [a["title"] or urlsplit(a["url"]).path for a in articles]
        corpus = "\n".join(f"{i + 1}. {t[:140]}" for i, t in enumerate(titles))
        try:
            data = await self.llm.json_completion(
                "You group OBSERVED article titles into 1-5 commercial themes. Do not invent articles. "
                'Return {"themes":[{"theme":str,"title_indexes":[int],"confidence":0..1}]}',
                f"Company: {company}\nTitles:\n{corpus}",
            )
        except FetchError:
            return []
        out = []
        for th in (data or {}).get("themes", [])[:5] if isinstance(data, dict) else []:
            if not isinstance(th, dict) or not isinstance(th.get("theme"), str):
                continue
            idx = [i for i in th.get("title_indexes", []) if isinstance(i, int) and 1 <= i <= len(titles)]
            if not idx:
                continue  # theme not grounded in observed titles -> dropped
            conf = th.get("confidence", 0.6)
            conf = min(float(conf) if isinstance(conf, (int, float)) else 0.6, LLM_CONFIDENCE_CAP)
            out.append({"theme": th["theme"][:60], "article_count": len(set(idx)), "confidence": conf})
        return out


def _iso_now() -> str:
    return iso(utcnow())
