"""Competitor Discovery Provider (spec #10).

Sources, in order:
  1. `known_competitors` supplied by the caller -> relationship_type=user_provided
  2. web search (Brave / Serper) over bounded queries built from the prospect's
     name, industry, location and products/services
  3. optional LLM entity resolution over the OBSERVED results. Every LLM-proposed
     competitor must be grounded: its name must appear in a result title/snippet
     and its domain must be one of the result hosts. Ungrounded names are dropped.
     Without an LLM a deterministic host-based fallback is used.

Never silently declares a competitor: every candidate carries relationship_type,
relationship_confidence, reason and evidence_refs. The prospect itself, and
directories/marketplaces/social networks, are excluded.
"""

from __future__ import annotations

from collections import defaultdict
from urllib.parse import urlsplit

from ..domain.errors import FetchError
from ..domain.identity import normalize_domain, normalize_name
from ..domain.models import ProviderResult
from ..domain.taxonomy import ClaimKind, ErrorCode, ObservationType, ProviderStatus, RelationshipType
from ..integrations.llm import LLMClient, NullLLM
from ..integrations.search import SearchClient, SearchResult
from .base import ResearchRequest, ResultBuilder

# Hosts that are never competitors (directories, marketplaces, social, media, search).
EXCLUDED_HOSTS = {
    "yelp.com",
    "tripadvisor.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "tiktok.com",
    "pinterest.com",
    "reddit.com",
    "quora.com",
    "medium.com",
    "wikipedia.org",
    "amazon.com",
    "ebay.com",
    "etsy.com",
    "google.com",
    "bing.com",
    "yahoo.com",
    "crunchbase.com",
    "g2.com",
    "capterra.com",
    "clutch.co",
    "trustpilot.com",
    "glassdoor.com",
    "indeed.com",
    "yellowpages.com",
    "bbb.org",
    "zomato.com",
    "foodpanda.pk",
    "ubereats.com",
    "doordash.com",
    "grubhub.com",
    "thumbtack.com",
    "angi.com",
    "houzz.com",
    "mapquest.com",
    "foursquare.com",
    "zoominfo.com",
    "owler.com",
    "similarweb.com",
    "producthunt.com",
    "alternativeto.net",
    "getapp.com",
    "softwareadvice.com",
    "trustradius.com",
    "forbes.com",
    "nytimes.com",
    "businessinsider.com",
    "timeout.com",
    "eater.com",
    "olx.com.pk",
    "daraz.pk",
}
LLM_REL_CAP = 0.85


def _host(url: str) -> str:
    return normalize_domain(urlsplit(url).hostname or "")


def _excluded(host: str) -> bool:
    return not host or any(host == h or host.endswith("." + h) for h in EXCLUDED_HOSTS)


def _mention_factor(n: int) -> float:
    return 0.8 if n <= 1 else (0.9 if n == 2 else 1.0)


class CompetitorDiscoveryProvider:
    name = "competitor_discovery"
    source_type = "web_search"
    scope = "prospect"

    def __init__(
        self, search: SearchClient, llm: LLMClient | None = None, *, max_queries: int = 4, max_candidates: int = 10
    ) -> None:
        self.search = search
        self.llm = llm or NullLLM()
        self.max_queries = max_queries
        self.max_candidates = max_candidates

    def _queries(self, req: ResearchRequest) -> list[str]:
        p = req.prospect
        loc = p.location or ""
        q = [f"{p.company_name} competitors", f"alternatives to {p.company_name}"]
        if p.industry:
            q.append(f"{p.industry} {loc}".strip())
        for ps in req.products_services[:2]:
            q.append(f"{ps} {loc}".strip())
        return list(dict.fromkeys(q))[: self.max_queries]

    async def collect(self, request: ResearchRequest) -> ProviderResult:
        b = ResultBuilder(self.name, self.source_type, request)
        prospect = request.prospect
        p_domain = normalize_domain(prospect.domain)
        p_name = normalize_name(prospect.company_name)
        candidates: dict[str, dict] = {}

        def is_self(name: str, domain: str) -> bool:
            return (bool(domain) and domain == p_domain) or normalize_name(name) == p_name

        # 1) caller-provided competitors
        for hint in request.context.get("known_competitors", []):
            name, dom = hint["company_name"], normalize_domain(hint.get("domain"))
            if is_self(name, dom):
                b.limit(f"Known competitor '{name}' matches the prospect itself and was ignored.")
                continue
            key = dom or f"name:{normalize_name(name)}"
            if key in candidates:
                continue
            eid = b.add_evidence(
                observation_type=ObservationType.COMPETITOR_CANDIDATES,
                claim=f"Caller identified {name} as a competitor of {prospect.company_name}",
                source_ref=f"user:{key}",
                source_type="caller_input",
                confidence=0.95,
                claim_kind=ClaimKind.FACT,
                raw={"company_name": name, "domain": dom or None},
            )
            candidates[key] = {
                "company_name": name,
                "domain": dom or None,
                "relationship_type": RelationshipType.USER_PROVIDED.value,
                "relationship_confidence": 0.95,
                "reason": "Named as a competitor by the caller",
                "evidence_refs": [eid],
                "discovered_via": "user",
                "mentions": 0,
            }

        # 2) search
        if not self.search.configured:
            b.limit("No search API configured (BRAVE_API_KEY or SERPER_API_KEY); automatic competitor discovery skipped.")
            status = ProviderStatus.PARTIAL if candidates else ProviderStatus.UNAVAILABLE
            if not candidates:
                b.error(ErrorCode.PROVIDER_NOT_CONFIGURED, "competitor discovery requires a search API or known_competitors")
            self._emit(b, candidates)
            return b.build(status)

        results: list[tuple[str, SearchResult]] = []
        for q in self._queries(request):
            try:
                rs = await self.search.search(q, count=10)
            except FetchError as e:
                b.error(e.code, f"search failed for one query: {e.message}")
                if e.code is ErrorCode.PROVIDER_NOT_CONFIGURED:
                    break
                continue
            results += [(q, r) for r in rs]
        if not results and not candidates:
            self._emit(b, candidates)
            return b.build(
                ProviderStatus.RATE_LIMITED
                if any(e.code is ErrorCode.RATE_LIMITED for e in b.errors)
                else ProviderStatus.UNAVAILABLE
            )

        by_host: dict[str, list[tuple[str, SearchResult]]] = defaultdict(list)
        for q, r in results:
            h = _host(r.url)
            if not _excluded(h) and h != p_domain:
                by_host[h].append((q, r))

        resolved = await self._llm_resolve(request, results) if self.llm.configured else None
        if resolved is None:
            if self.llm.configured:
                b.limit("LLM entity resolution unavailable; deterministic host-based fallback used.")
            # deterministic fallback: each non-excluded host that ranks in the results
            for host, hits in sorted(
                by_host.items(), key=lambda kv: (-len({q for q, _ in kv[1]}), min(r.rank for _, r in kv[1]))
            ):
                title = hits[0][1].title.split(" | ")[0].split(" - ")[0].strip()[:80] or host
                if is_self(title, host) or host in candidates:
                    continue
                queries = {q for q, _ in hits}
                rel = RelationshipType.LIKELY_COMPETITOR if len(queries) >= 2 else RelationshipType.CANDIDATE
                conf = 0.55 if len(queries) >= 2 else 0.4
                eid = self._search_evidence(b, title, host, hits, prospect.company_name, rel, conf)
                candidates[host] = {
                    "company_name": title,
                    "domain": host,
                    "relationship_type": rel.value,
                    "relationship_confidence": conf,
                    "reason": f"Business site ranking for {len(queries)} market query(ies): " + "; ".join(sorted(queries))[:200],
                    "evidence_refs": [eid],
                    "discovered_via": "search",
                    "mentions": len(queries),
                }
        else:
            resolve_budget = 3
            for c in resolved:
                host = c["domain"]
                if not host and resolve_budget > 0:
                    resolve_budget -= 1
                    host = await self._resolve_domain(c["company_name"], p_domain)
                if not host:
                    b.limit(f"Official domain for '{c['company_name']}' could not be resolved; candidate dropped.")
                    continue
                if is_self(c["company_name"], host) or host in candidates or _excluded(host):
                    continue
                hits = by_host.get(host) or [
                    (q, r) for q, r in results if c["company_name"].lower() in (r.title + " " + r.snippet).lower()
                ]
                queries = {q for q, _ in hits}
                conf = min(c["confidence"], LLM_REL_CAP) * _mention_factor(len(queries))
                rel = RelationshipType(c["relationship_type"])
                eid = self._search_evidence(
                    b, c["company_name"], host, hits, prospect.company_name, rel, conf, reason=c["reason"]
                )
                candidates[host] = {
                    "company_name": c["company_name"],
                    "domain": host,
                    "relationship_type": rel.value,
                    "relationship_confidence": round(conf, 3),
                    "reason": c["reason"][:300],
                    "evidence_refs": [eid],
                    "discovered_via": "search+llm",
                    "mentions": len(queries),
                }

        self._emit(b, candidates)
        return b.build()

    def _search_evidence(self, b, name, host, hits, prospect_name, rel, conf, reason: str | None = None) -> str:
        top = hits[0][1] if hits else None
        return b.add_evidence(
            observation_type=ObservationType.COMPETITOR_CANDIDATES,
            claim=f"{name} ({host}) classified as {rel.value} of {prospect_name} (confidence {conf:.2f})",
            source_ref=f"search:{host}",
            source_url=top.url if top else None,
            claim_kind=ClaimKind.INFERENCE,
            confidence=conf,
            raw={
                "queries": sorted({q for q, _ in hits})[:4],
                "title": top.title if top else None,
                "snippet": top.snippet if top else None,
                "reason": reason,
            },
        )

    def _emit(self, b: ResultBuilder, candidates: dict[str, dict]) -> None:
        ranked = sorted(candidates.values(), key=lambda c: (c["discovered_via"] != "user", -c["relationship_confidence"]))
        ranked = ranked[: self.max_candidates]
        b.add_observation(
            ObservationType.COMPETITOR_CANDIDATES,
            metrics={"candidate_count": len(ranked), "user_provided": sum(1 for c in ranked if c["discovered_via"] == "user")},
            items=ranked,
            evidence_refs=[e for c in ranked for e in c["evidence_refs"]],
            claim_kind=ClaimKind.INFERENCE,
        )

    async def _resolve_domain(self, name: str, prospect_domain: str) -> str:
        """One bounded search to find a company's own site (host must contain a name token)."""
        tokens = [t for t in normalize_name(name).split() if len(t) > 2]
        try:
            rs = await self.search.search(f"{name} official site", count=5)
        except FetchError:
            return ""
        for r in rs:
            h = _host(r.url)
            if h and h != prospect_domain and not _excluded(h) and any(t in h for t in tokens):
                return h
        return ""

    async def _llm_resolve(self, req: ResearchRequest, results: list[tuple[str, SearchResult]]) -> list[dict] | None:
        rows = results[:30]
        corpus = "\n".join(f"[{i + 1}] {r.title} | {r.snippet[:220]} | {r.url}" for i, (_, r) in enumerate(rows))
        p = req.prospect
        try:
            data = await self.llm.json_completion(
                "You identify competitor COMPANIES in observed web search results. Only use companies that appear in "
                "the results. Exclude the prospect, directories, marketplaces, media and review sites. "
                'Return {"competitors":[{"company_name":str,"result_index":int,"relationship_type":'
                '"direct_competitor"|"likely_competitor"|"adjacent_player","confidence":0..1,"reason":str}]}',
                f"Prospect: {p.company_name} | industry: {p.industry or '?'} | location: {p.location or '?'} | "
                f"domain: {p.domain or '?'} | offers: {', '.join(req.products_services) or '?'}\nResults:\n{corpus}",
            )
        except FetchError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("competitors"), list):
            return None
        out = []
        for c in data["competitors"][:15]:
            if not isinstance(c, dict) or not isinstance(c.get("company_name"), str):
                continue
            idx = c.get("result_index")
            if not isinstance(idx, int) or not 1 <= idx <= len(rows):
                continue
            _, r = rows[idx - 1]
            name = c["company_name"].strip()[:100]
            # grounding check: name must be visible in the cited result
            if name.lower() not in (r.title + " " + r.snippet + " " + r.url).lower() and normalize_name(name).replace(
                " ", ""
            ) not in _host(r.url):
                continue
            host = _host(r.url)
            tokens = [t for t in normalize_name(name).split() if len(t) > 2]
            if _excluded(host) or not any(t in host for t in tokens):
                host = ""  # result is about the company, not its own site -> resolve separately
            rel = c.get("relationship_type")
            if rel not in ("direct_competitor", "likely_competitor", "adjacent_player"):
                rel = "candidate"
            conf = c.get("confidence")
            out.append(
                {
                    "company_name": name,
                    "domain": host,
                    "relationship_type": rel,
                    "confidence": float(conf) if isinstance(conf, (int, float)) else 0.5,
                    "reason": str(c.get("reason") or "LLM classification of observed search result")[:300],
                }
            )
        return out
