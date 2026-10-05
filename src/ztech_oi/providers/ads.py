"""Advertising Intelligence Providers (spec #11, #12).

MetaAdsProvider — OFFICIAL Meta Ad Library API (graph.facebook.com/ads_archive).
  Needs META_ACCESS_TOKEN (free; requires identity verification at
  facebook.com/ID). IMPORTANT coverage rule enforced in code: the API returns
  ALL ad types only for ads delivered in the EU/UK; elsewhere it returns only
  political/issue ads. Therefore a ZERO count from a non-EU query is reported
  as `coverage_reliable=0` and comparisons treat it as unknown, not as "no ads".

GoogleAdsProvider — Google Ads Transparency Center has no official API. When
  SERPAPI_API_KEY is set we read it through SerpApi's
  `google_ads_transparency_center` engine and label every item as a
  THIRD-PARTY-collected observation. Without a key: UNAVAILABLE.

Neither provider ever reports spend, impressions or performance — those are not
observable. Counts are counts of creatives actually listed.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..domain.errors import FetchError, UnsafeTarget
from ..domain.identity import iso, normalize_domain, normalize_name, parse_iso, utcnow
from ..domain.models import ProviderResult
from ..domain.taxonomy import ErrorCode, ObservationType, ProviderStatus
from ..net.http import SafeHttpClient
from ..net.parsing import extract_offers, keywords, topic_matches
from .base import ResearchRequest, ResultBuilder, simple_result

EU_UK = {
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IE",
    "IT",
    "LV",
    "LT",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
    "GB",
}
META_FIELDS = (
    "id,page_id,page_name,ad_creation_time,ad_delivery_start_time,ad_delivery_stop_time,ad_creative_bodies,"
    "ad_creative_link_titles,ad_creative_link_captions,ad_creative_link_descriptions,ad_snapshot_url,"
    "publisher_platforms,languages"
)
NO_SPEND = "Spend, impressions and performance are not observable from this source and are never reported."


def _strip_token(url: str | None) -> str | None:
    """ad_snapshot_url embeds the access token -> remove it before storing."""
    if not url:
        return None
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in ("access_token", "token")]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


def name_match(entity_name: str, candidate: str) -> float:
    a, b = set(normalize_name(entity_name).split()), set(normalize_name(candidate).split())
    if not a or not b:
        return 0.0
    if normalize_name(entity_name) == normalize_name(candidate):
        return 0.95
    overlap = len(a & b) / len(a)
    return 0.75 if overlap >= 0.67 else (0.5 if overlap >= 0.5 else 0.0)


def _themes(texts: list[str], top: int = 8) -> list[dict]:
    c: Counter[str] = Counter()
    for t in texts:
        c.update(set(keywords(t)))
    return [{"term": k, "creatives": v} for k, v in c.most_common(top) if v >= 2]


class MetaAdsProvider:
    name = "meta_ads"
    source_type = "meta_ad_library_api"
    scope = "per_entity"
    ENDPOINT = "https://graph.facebook.com/v21.0/ads_archive"

    def __init__(self, http: SafeHttpClient, access_token: str | None, countries: list[str], *, max_ads: int = 50) -> None:
        self.http = http
        self._token = access_token
        self.countries = countries or ["US"]
        self.max_ads = max_ads

    async def collect(self, request: ResearchRequest) -> ProviderResult:
        if not self._token:
            return simple_result(
                self.name,
                self.source_type,
                request,
                ProviderStatus.UNAVAILABLE,
                ErrorCode.PROVIDER_NOT_CONFIGURED,
                "Meta Ad Library API not configured (set META_ACCESS_TOKEN)",
                ["Meta provider: NOT CONFIGURED — no Meta advertising data was collected."],
            )
        b = ResultBuilder(self.name, self.source_type, request)
        b.limit(NO_SPEND)
        reliable = all(c in EU_UK for c in self.countries)
        if not reliable:
            b.limit(
                "Meta Ad Library API returns non-political ads only for EU/UK delivery; for "
                f"{','.join(self.countries)} a zero count is NOT evidence of no advertising."
            )
        params = {
            "access_token": self._token,
            "search_terms": request.entity.company_name[:100],
            "ad_reached_countries": json.dumps(self.countries),
            "ad_active_status": "ACTIVE",
            "ad_type": "ALL",
            "fields": META_FIELDS,
            "limit": str(min(self.max_ads, 100)),
        }
        try:
            resp = await self.http.get(self.ENDPOINT, params=params)
        except (FetchError, UnsafeTarget) as e:
            b.error(e.code, f"Meta Ad Library request failed: {e.message}")
            return b.build(ProviderStatus.RATE_LIMITED if e.code is ErrorCode.RATE_LIMITED else ProviderStatus.UNAVAILABLE)
        data = resp.json() if resp.content else {}
        if not isinstance(data, dict):
            b.error(ErrorCode.MALFORMED_RESPONSE, "unexpected Meta response shape")
            return b.build(ProviderStatus.FAILED)
        if "error" in data:
            err = data["error"] if isinstance(data["error"], dict) else {}
            code = err.get("code")
            if code in (4, 17, 32, 613):
                b.error(ErrorCode.RATE_LIMITED, "Meta API rate limit")
                return b.build(ProviderStatus.RATE_LIMITED)
            if code in (190, 10, 200, 2332002):
                b.error(ErrorCode.PROVIDER_NOT_CONFIGURED, f"Meta API rejected token/permission (code {code})")
                return b.build(ProviderStatus.UNAVAILABLE)
            b.error(ErrorCode.SOURCE_UNAVAILABLE, f"Meta API error (code {code})")
            return b.build(ProviderStatus.FAILED)
        rows = data.get("data")
        if not isinstance(rows, list):
            b.error(ErrorCode.MALFORMED_RESPONSE, "Meta response missing 'data' list")
            return b.build(ProviderStatus.FAILED)

        domain = normalize_domain(request.entity.domain)
        matched: list[dict] = []
        unmatched_pages: set[str] = set()
        for ad in rows[: self.max_ads]:
            if not isinstance(ad, dict):
                continue
            page = str(ad.get("page_name") or "")
            captions = [str(c) for c in (ad.get("ad_creative_link_captions") or []) if c]
            score = name_match(request.entity.company_name, page)
            if domain and any(domain in normalize_domain(c) or domain in c.lower() for c in captions):
                score = max(score, 0.9)
            if score < 0.5:
                unmatched_pages.add(page)
                continue
            matched.append({"ad": ad, "match": score})
        if unmatched_pages:
            b.limit(
                f"{len(unmatched_pages)} advertiser page(s) returned by the search did not match the entity and were excluded."
            )

        now = utcnow()
        topics = request.products_services
        refs: list[str] = []
        items: list[dict] = []
        texts: list[str] = []
        started_30 = 0
        ctas: Counter[str] = Counter()
        landings: Counter[str] = Counter()
        offer_creatives = relevant = 0
        for m in matched:
            ad = m["ad"]
            start = parse_iso(str(ad.get("ad_delivery_start_time") or ""))
            if start and now - start <= timedelta(days=30):
                started_30 += 1
            bodies = [str(x)[:300] for x in (ad.get("ad_creative_bodies") or [])][:2]
            titles = [str(x)[:120] for x in (ad.get("ad_creative_link_titles") or [])][:2]
            captions = [str(x)[:120] for x in (ad.get("ad_creative_link_captions") or [])][:2]
            descs = [str(x)[:200] for x in (ad.get("ad_creative_link_descriptions") or [])][:2]
            text = " ".join(bodies + titles + descs)
            texts += bodies + titles
            offers = extract_offers(text)
            matched_topics = topic_matches(text, topics) if topics else []
            offer_creatives += 1 if offers else 0
            relevant += 1 if matched_topics else 0
            for t in titles:
                ctas[t] += 1
            for c in captions:
                d = normalize_domain(c) or c.lower()
                landings[d] += 1
            snapshot = _strip_token(ad.get("ad_snapshot_url"))
            refs.append(
                b.add_evidence(
                    observation_type=ObservationType.ADS_META,
                    claim=f'Active Meta ad by page "{ad.get("page_name")}" delivering since {iso(start)[:10] if start else "unknown date"}',
                    source_ref=f"meta_ad:{ad.get('id')}",
                    source_url=snapshot,
                    confidence=round(0.95 * m["match"], 3),
                    observed_at=iso(start) if start else None,
                    raw={
                        "ad_id": ad.get("id"),
                        "page_name": ad.get("page_name"),
                        "body": bodies[0] if bodies else None,
                        "link_title": titles[0] if titles else None,
                        "caption": captions[0] if captions else None,
                        "platforms": ad.get("publisher_platforms"),
                        "entity_match": m["match"],
                        "offers": offers[:2],
                        "topics": matched_topics,
                    },
                )
            )
            items.append(
                {
                    "ad_id": ad.get("id"),
                    "page_name": ad.get("page_name"),
                    "started_at": iso(start) if start else None,
                    "body": bodies[0] if bodies else None,
                    "link_title": titles[0] if titles else None,
                    "landing_caption": captions[0] if captions else None,
                    "platforms": ad.get("publisher_platforms"),
                    "snapshot_url": snapshot,
                    "offers": offers[:2],
                    "topics": matched_topics,
                }
            )

        # creative type: the Ad Library exposes media type only as a search FILTER, so one bounded
        # extra query (media_type=VIDEO) counts video creatives; the rest are reported as non-video.
        video_count: int | None = None
        if matched:
            vparams = {**params, "media_type": "VIDEO", "fields": "id"}
            try:
                vresp = await self.http.get(self.ENDPOINT, params=vparams)
                vdata = vresp.json() if vresp.content else {}
                vrows = vdata.get("data") if isinstance(vdata, dict) else None
                if isinstance(vrows, list):
                    ids = {str(m["ad"].get("id")) for m in matched}
                    video_count = sum(1 for r in vrows if isinstance(r, dict) and str(r.get("id")) in ids)
                    for it in items:
                        it["media"] = (
                            "video"
                            if str(it["ad_id"]) in {str(r.get("id")) for r in vrows if isinstance(r, dict)}
                            else "non_video"
                        )
                else:
                    b.limit("Video/non-video breakdown unavailable (unexpected response to media_type filter).")
            except (FetchError, UnsafeTarget):
                b.limit("Video/non-video breakdown unavailable (media_type query failed).")

        summary = b.add_evidence(
            observation_type=ObservationType.ADS_META,
            claim=(
                f"{len(matched)} active Meta creative(s) attributed to {request.entity.company_name} in the Ad Library "
                f"for {','.join(self.countries)} at capture time"
            ),
            source_ref=f"meta_summary:{','.join(self.countries)}",
            source_url="https://www.facebook.com/ads/library/",
            confidence=0.9 if reliable else 0.6,
            metric="active_creative_count",
            value=len(matched),
            raw={"countries": self.countries, "coverage_reliable": reliable, "returned": len(rows)},
        )
        metric_refs = [summary]
        if topics and matched:
            metric_refs.append(
                b.add_evidence(
                    observation_type=ObservationType.ADS_META,
                    claim=f"{relevant} of {len(matched)} active Meta creative(s) mention {', '.join(topics[:3])}",
                    source_ref=f"meta_relevant:{'|'.join(topics)}",
                    source_url="https://www.facebook.com/ads/library/",
                    confidence=0.8 if reliable else 0.55,
                    metric="relevant_creative_count",
                    value=relevant,
                    raw={"topics": topics, "method": "deterministic keyword match on creative text"},
                )
            )
        if isinstance(data.get("paging"), dict) and data["paging"].get("next"):
            b.limit(f"Result set truncated at {self.max_ads} ads (pagination bounded).")
        attributes = {
            "themes": _themes(texts),
            "ctas": [{"cta": k, "creatives": v} for k, v in ctas.most_common(8)],
            "landing_domains": [{"domain": k, "creatives": v} for k, v in landings.most_common(8)],
        }
        b.add_observation(
            ObservationType.ADS_META,
            metrics={
                "active_creative_count": len(matched),
                "started_last_30d": started_30,
                "distinct_pages": len({i["page_name"] for i in items}),
                "coverage_reliable": 1 if reliable else 0,
                "offer_creative_count": offer_creatives,
                "relevant_creative_count": relevant if topics else None,
                "video_creative_count": video_count,
                "non_video_creative_count": (len(matched) - video_count) if video_count is not None else None,
                "distinct_ctas": len(ctas),
                "distinct_landing_domains": len(landings),
            },
            items=items + [attributes] if items else [],
            evidence_refs=[*metric_refs, *refs],
        )
        return b.build()


class GoogleAdsProvider:
    name = "google_ads"
    source_type = "google_ads_transparency_via_serpapi"
    scope = "per_entity"
    ENDPOINT = "https://serpapi.com/search.json"

    def __init__(self, http: SafeHttpClient, serpapi_key: str | None, *, region: str | None = None, max_ads: int = 50) -> None:
        self.http = http
        self._key = serpapi_key
        self.region = region
        self.max_ads = max_ads

    async def collect(self, request: ResearchRequest) -> ProviderResult:
        if not self._key:
            return simple_result(
                self.name,
                self.source_type,
                request,
                ProviderStatus.UNAVAILABLE,
                ErrorCode.PROVIDER_NOT_CONFIGURED,
                "Google Ads Transparency has no official API; set SERPAPI_API_KEY to enable the third-party adapter",
                ["Google provider: NOT CONFIGURED — no Google advertising data was collected."],
            )
        domain = normalize_domain(request.entity.domain)
        if not domain:
            return simple_result(
                self.name,
                self.source_type,
                request,
                ProviderStatus.UNAVAILABLE,
                ErrorCode.INSUFFICIENT_EVIDENCE,
                "entity has no domain to search the transparency center",
            )
        b = ResultBuilder(self.name, self.source_type, request)
        b.limit(NO_SPEND)
        b.limit(
            "Collected via SerpApi (third party) from Google Ads Transparency Center; listing completeness depends on that service."
        )
        params = {"engine": "google_ads_transparency_center", "text": domain, "api_key": self._key, "num": str(self.max_ads)}
        if self.region:
            params["region"] = self.region
        try:
            resp = await self.http.get(self.ENDPOINT, params=params)
        except (FetchError, UnsafeTarget) as e:
            b.error(e.code, f"SerpApi request failed: {e.message}")
            return b.build(ProviderStatus.RATE_LIMITED if e.code is ErrorCode.RATE_LIMITED else ProviderStatus.UNAVAILABLE)
        if resp.status in (401, 403):
            b.error(ErrorCode.PROVIDER_NOT_CONFIGURED, "SerpApi rejected the API key")
            return b.build(ProviderStatus.UNAVAILABLE)
        data = resp.json()
        if not isinstance(data, dict):
            b.error(ErrorCode.MALFORMED_RESPONSE, "unexpected SerpApi response")
            return b.build(ProviderStatus.FAILED)
        if data.get("error") and "hasn't returned any results" not in str(data.get("error")):
            b.error(ErrorCode.SOURCE_UNAVAILABLE, f"SerpApi error: {str(data.get('error'))[:120]}")
            return b.build(ProviderStatus.FAILED)
        rows = data.get("ad_creatives") or []
        if not isinstance(rows, list):
            b.error(ErrorCode.MALFORMED_RESPONSE, "ad_creatives is not a list")
            return b.build(ProviderStatus.FAILED)
        now = utcnow()
        refs: list[str] = []
        items: list[dict] = []
        formats: Counter[str] = Counter()
        landings: Counter[str] = Counter()
        texts: list[str] = []
        topics = request.products_services
        recent = offer_creatives = relevant = with_copy = 0
        for ad in rows[: self.max_ads]:
            if not isinstance(ad, dict):
                continue
            target = normalize_domain(str(ad.get("target_domain") or ""))
            adv = str(ad.get("advertiser") or "")
            match = 0.95 if target == domain else name_match(request.entity.company_name, adv)
            if match < 0.5:
                continue
            first = _ts(ad.get("first_shown"))
            last = _ts(ad.get("last_shown"))
            if last and now - last <= timedelta(days=30):
                recent += 1
            formats[str(ad.get("format") or "unknown")] += 1
            copy = _ad_copy(ad)
            text = " ".join(copy.values())
            offers = extract_offers(text) if text else []
            matched_topics = topic_matches(text, topics) if (text and topics) else []
            with_copy += 1 if text else 0
            offer_creatives += 1 if offers else 0
            relevant += 1 if matched_topics else 0
            texts.append(text)
            landing = normalize_domain(str(ad.get("link") or copy.get("displayed_link") or "")) or target
            if landing:
                landings[landing] += 1
            refs.append(
                b.add_evidence(
                    observation_type=ObservationType.ADS_GOOGLE,
                    claim=f'Google ad creative ({ad.get("format", "unknown")}) by "{adv}" listed; first shown {iso(first)[:10] if first else "?"}, last shown {iso(last)[:10] if last else "?"}',
                    source_ref=f"google_ad:{ad.get('ad_creative_id')}",
                    source_url=ad.get("details_link"),
                    source_type="third_party:serpapi",
                    confidence=round(0.85 * match, 3),
                    observed_at=iso(first) if first else None,
                    raw={
                        "advertiser": adv,
                        "format": ad.get("format"),
                        "target_domain": target,
                        "entity_match": match,
                        "copy": copy,
                        "offers": offers[:2],
                        "topics": matched_topics,
                    },
                )
            )
            items.append(
                {
                    "creative_id": ad.get("ad_creative_id"),
                    "advertiser": adv,
                    "format": ad.get("format"),
                    "first_shown": iso(first) if first else None,
                    "last_shown": iso(last) if last else None,
                    "target_domain": target,
                    "landing_domain": landing or None,
                    "copy": copy or None,
                    "offers": offers[:2],
                    "topics": matched_topics,
                }
            )
        summary = b.add_evidence(
            observation_type=ObservationType.ADS_GOOGLE,
            claim=f"{len(items)} Google ad creative(s) listed for {domain} in the Ads Transparency Center (via SerpApi); {recent} shown in the last 30 days",
            source_ref=f"google_summary:{domain}",
            source_url="https://adstransparency.google.com/",
            source_type="third_party:serpapi",
            confidence=0.8,
            metric="creative_count",
            value=len(items),
            raw={"formats": dict(formats), "recent_30d": recent},
        )
        metric_refs = [summary]
        if items and with_copy == 0:
            b.limit(
                "SerpApi listing returned no ad copy for these creatives; themes, offers and topic relevance are unknown (null)."
            )
        if topics and with_copy:
            metric_refs.append(
                b.add_evidence(
                    observation_type=ObservationType.ADS_GOOGLE,
                    claim=f"{relevant} of {with_copy} Google creative(s) with readable copy mention {', '.join(topics[:3])}",
                    source_ref=f"google_relevant:{'|'.join(topics)}",
                    source_type="third_party:serpapi",
                    confidence=0.7,
                    metric="relevant_creative_count",
                    value=relevant,
                )
            )
        attributes = {
            "themes": _themes([t for t in texts if t]),
            "landing_domains": [{"domain": k, "creatives": v} for k, v in landings.most_common(8)],
        }
        b.add_observation(
            ObservationType.ADS_GOOGLE,
            metrics={
                "creative_count": len(items),
                "shown_last_30d": recent,
                "text_ads": formats.get("text", 0),
                "image_ads": formats.get("image", 0),
                "video_ads": formats.get("video", 0),
                "creatives_with_copy": with_copy,
                "offer_creative_count": offer_creatives if with_copy else None,
                "relevant_creative_count": relevant if (topics and with_copy) else None,
            },
            items=items + [attributes] if items else [],
            evidence_refs=[*metric_refs, *refs],
        )
        return b.build()


_COPY_KEYS = ("title", "headline", "snippet", "description", "body", "text", "displayed_link", "call_to_action")


def _ad_copy(ad: dict) -> dict[str, str]:
    """Defensive extraction of ad copy fields from a SerpApi transparency-center creative.

    Field names vary by creative format; only string fields we recognise are kept (bounded).
    """
    out: dict[str, str] = {}
    for k in _COPY_KEYS:
        v = ad.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:300]
        elif isinstance(v, list):
            joined = " ".join(str(x) for x in v if isinstance(x, str))[:300]
            if joined:
                out[k] = joined
    return out


def _ts(v: object) -> datetime | None:
    if isinstance(v, (int, float)) and v > 0:
        return datetime.fromtimestamp(v, tz=UTC)
    if isinstance(v, str):
        return parse_iso(v)
    return None
