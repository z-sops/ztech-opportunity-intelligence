"""Competitive Comparison Engine (spec #20) — deterministic primitives.

Rules:
  * a side with no observation (provider unavailable/unsupported/failed) -> null
  * a ZERO that cannot be trusted is converted to null, never to "inactive":
      - Meta zero when coverage is not reliable for the queried countries
      - website-inventory zeros when the inventory came from homepage links
        (no sitemap) — a partial inventory cannot prove absence
  * null on either side -> insufficient_evidence (confidence 0)
  * "more active" requires BOTH a ratio >= 1.5 AND an absolute delta >= the
    dimension's minimum, so 1 vs 2 is parity, not a gap.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..domain.identity import stable_id
from ..domain.models import Comparison, Entity
from ..domain.taxonomy import ComparisonDimension, ComparisonInterpretation, ObservationType
from .index import RunIndex

Extractor = Callable[[RunIndex, str], float | None]


@dataclass(frozen=True)
class DimensionSpec:
    dimension: ComparisonDimension
    obs_type: ObservationType
    metric: str
    unit: str
    window: str | None
    min_delta: float
    extract: Extractor
    topic_scoped: bool = False
    alt_obs: ObservationType | None = None


def _plain(obs_type: ObservationType, metric: str) -> Extractor:
    return lambda ix, eid: ix.metric(eid, obs_type, metric)


def _meta(ix: RunIndex, eid: str) -> float | None:
    v = ix.metric(eid, ObservationType.ADS_META, "active_creative_count")
    if v is None:
        return None
    if v == 0 and ix.metric(eid, ObservationType.ADS_META, "coverage_reliable") == 0:
        return None
    return v


def _inventory(metric: str) -> Extractor:
    def f(ix: RunIndex, eid: str) -> float | None:
        v = ix.metric(eid, ObservationType.WEBSITE_PAGE_INVENTORY, metric)
        if v is None:
            return None
        if v == 0 and ix.metric(eid, ObservationType.WEBSITE_PAGE_INVENTORY, "inventory_from_sitemap") != 1:
            return None
        return v

    return f


def _sum_inventory(*metrics: str) -> Extractor:
    def f(ix: RunIndex, eid: str) -> float | None:
        vals = [ix.metric(eid, ObservationType.WEBSITE_PAGE_INVENTORY, m) for m in metrics]
        if any(v is None for v in vals):
            return None
        total = float(sum(v for v in vals if v is not None))
        if total == 0 and ix.metric(eid, ObservationType.WEBSITE_PAGE_INVENTORY, "inventory_from_sitemap") != 1:
            return None
        return total

    return f


def _hiring(ix: RunIndex, eid: str) -> float | None:
    v = _inventory("careers_page_count")(ix, eid)
    return None if v is None else (1.0 if v > 0 else 0.0)


def _coverage(ix: RunIndex, eid: str) -> float | None:
    if ix.metric(eid, ObservationType.WEBSITE_PAGE_INVENTORY, "inventory_from_sitemap") != 1:
        return None  # page totals are only comparable when both came from sitemaps
    return ix.metric(eid, ObservationType.WEBSITE_PAGE_INVENTORY, "page_count")


def _relevant_ads(ix: RunIndex, eid: str) -> float | None:
    """Topic-relevant creatives across Meta (only when coverage is trustworthy or >0) and Google."""
    parts: list[float] = []
    meta = ix.metric(eid, ObservationType.ADS_META, "relevant_creative_count")
    if meta is not None and not (meta == 0 and ix.metric(eid, ObservationType.ADS_META, "coverage_reliable") == 0):
        parts.append(meta)
    google = ix.metric(eid, ObservationType.ADS_GOOGLE, "relevant_creative_count")
    if google is not None:
        parts.append(google)
    return float(sum(parts)) if parts else None


def _offers(ix: RunIndex, eid: str) -> float | None:
    pricing = _inventory("pricing_page_count")(ix, eid)
    landing = _inventory("landing_page_count")(ix, eid)
    mentions = ix.metric(eid, ObservationType.WEBSITE_COMPANY_PROFILE, "offer_mentions")
    parts = [p for p in (pricing, landing, mentions) if p is not None]
    return float(sum(parts)) if parts else None


DIMENSIONS: list[DimensionSpec] = [
    DimensionSpec(
        ComparisonDimension.META_CREATIVE_ACTIVITY,
        ObservationType.ADS_META,
        "active_creative_count",
        "active_creatives",
        "at_capture",
        3,
        _meta,
    ),
    DimensionSpec(
        ComparisonDimension.GOOGLE_AD_ACTIVITY,
        ObservationType.ADS_GOOGLE,
        "creative_count",
        "listed_creatives",
        "transparency_center_listing",
        3,
        _plain(ObservationType.ADS_GOOGLE, "creative_count"),
    ),
    DimensionSpec(
        ComparisonDimension.CONTENT_PRODUCTION_60D,
        ObservationType.CONTENT_INVENTORY,
        "articles_last_60d",
        "articles",
        "last_60_days",
        2,
        _plain(ObservationType.CONTENT_INVENTORY, "articles_last_60d"),
    ),
    DimensionSpec(
        ComparisonDimension.COMMERCIAL_CONTENT,
        ObservationType.WEBSITE_PAGE_INVENTORY,
        "commercial_page_count",
        "pages",
        "at_capture",
        3,
        _inventory("commercial_page_count"),
    ),
    DimensionSpec(
        ComparisonDimension.LANDING_PAGE_ACTIVITY,
        ObservationType.WEBSITE_PAGE_INVENTORY,
        "landing_page_count",
        "pages",
        "at_capture",
        2,
        _sum_inventory("landing_page_count"),
    ),
    DimensionSpec(
        ComparisonDimension.OFFERS,
        ObservationType.WEBSITE_COMPANY_PROFILE,
        "offer_mentions",
        "offer_signals",
        "at_capture",
        2,
        _offers,
    ),
    DimensionSpec(
        ComparisonDimension.HIRING,
        ObservationType.WEBSITE_PAGE_INVENTORY,
        "careers_page_count",
        "careers_page_present",
        "at_capture",
        1,
        _hiring,
    ),
    DimensionSpec(
        ComparisonDimension.SOCIAL_ACTIVITY_30D,
        ObservationType.SOCIAL_TWITTER,
        "posts_last_30d",
        "x_posts",
        "last_30_days",
        4,
        _plain(ObservationType.SOCIAL_TWITTER, "posts_last_30d"),
    ),
    DimensionSpec(
        ComparisonDimension.SEO_CONTENT_COVERAGE,
        ObservationType.WEBSITE_PAGE_INVENTORY,
        "page_count",
        "indexable_pages_in_sitemap",
        "at_capture",
        15,
        _coverage,
    ),
    DimensionSpec(
        ComparisonDimension.EXPANSION,
        ObservationType.WEBSITE_PAGE_INVENTORY,
        "location_page_count",
        "location_pages",
        "at_capture",
        2,
        _inventory("location_page_count"),
    ),
]
DIMENSIONS += [
    DimensionSpec(
        ComparisonDimension.RELEVANT_CONTENT_60D,
        ObservationType.CONTENT_INVENTORY,
        "relevant_articles_last_60d",
        "topic_articles",
        "last_60_days",
        2,
        _plain(ObservationType.CONTENT_INVENTORY, "relevant_articles_last_60d"),
        topic_scoped=True,
    ),
    DimensionSpec(
        ComparisonDimension.RELEVANT_AD_CREATIVES,
        ObservationType.ADS_META,
        "relevant_creative_count",
        "topic_ad_creatives",
        "at_capture",
        2,
        _relevant_ads,
        topic_scoped=True,
        alt_obs=ObservationType.ADS_GOOGLE,
    ),
]
SPEC_BY_DIM = {s.dimension: s for s in DIMENSIONS}


def interpret(p: float | None, c: float | None, min_delta: float) -> tuple[ComparisonInterpretation, float]:
    """Return (interpretation, magnitude factor 0..1)."""
    if p is None or c is None:
        return ComparisonInterpretation.INSUFFICIENT_EVIDENCE, 0.0
    hi, lo = max(p, c), min(p, c)
    if hi - lo < min_delta or (lo > 0 and hi / lo < 1.5):
        return ComparisonInterpretation.PARITY, 0.7
    ratio = hi / lo if lo > 0 else 3.0
    magnitude = min(1.0, 0.7 + 0.15 * (min(ratio, 3.0) - 1.0))
    who = ComparisonInterpretation.COMPETITOR_MORE_ACTIVE if c > p else ComparisonInterpretation.PROSPECT_MORE_ACTIVE
    return who, magnitude


class ComparisonEngine:
    def build(
        self, research_id: str, ix: RunIndex, prospect: Entity, competitors: Sequence[Entity], topic: str | None = None
    ) -> list[Comparison]:
        """`topic` (the prospect's products/services) enables topic-scoped dimensions; without it they are skipped."""
        out: list[Comparison] = []

        def conf(eid: str, spec: DimensionSpec) -> float:
            vals = [ix.metric_confidence(eid, t, spec.metric) for t in (spec.obs_type, spec.alt_obs) if t is not None]
            vals = [v for v in vals if v > 0]
            return max(vals) if vals else 0.0

        def refs_of(eid: str, spec: DimensionSpec) -> list[str]:
            r = ix.refs_for(eid, spec.obs_type, spec.metric, 2)
            if spec.alt_obs is not None:
                r += ix.refs_for(eid, spec.alt_obs, spec.metric, 2)
            return r

        for comp in competitors:
            for spec in DIMENSIONS:
                if spec.topic_scoped and not topic:
                    continue
                p = spec.extract(ix, prospect.entity_id)
                c = spec.extract(ix, comp.entity_id)
                interp, mag = interpret(p, c, spec.min_delta)
                if interp is ComparisonInterpretation.INSUFFICIENT_EVIDENCE:
                    confidence = 0.0
                    missing = []
                    if p is None:
                        missing.append(f"prospect ({ix.status(prospect.entity_id, _provider(spec)) or 'not run'})")
                    if c is None:
                        missing.append(f"{comp.company_name} ({ix.status(comp.entity_id, _provider(spec)) or 'not run'})")
                    note = "No trustworthy observation for " + " and ".join(missing) + "; no gap or parity is inferred."
                else:
                    confidence = round(min(conf(prospect.entity_id, spec), conf(comp.entity_id, spec)) * mag, 3)
                    note = None
                refs = refs_of(prospect.entity_id, spec) + refs_of(comp.entity_id, spec)
                out.append(
                    Comparison(
                        comparison_id=stable_id("cmp", research_id, comp.entity_id, spec.dimension.value),
                        dimension=spec.dimension,
                        prospect_id=prospect.entity_id,
                        competitor_id=comp.entity_id,
                        prospect_observed=p,
                        competitor_observed=c,
                        unit=spec.unit,
                        topic=topic if spec.topic_scoped else None,
                        window=spec.window,
                        interpretation=interp,
                        confidence=confidence,
                        evidence_refs=refs if p is not None or c is not None else [],
                        note=note,
                    )
                )
        return out


def _provider(spec: DimensionSpec) -> str:
    from .index import OBS_PROVIDER

    return OBS_PROVIDER[spec.obs_type]
