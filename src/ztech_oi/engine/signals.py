"""Signal Engine (spec #18): evidence/observations/changes/comparisons -> controlled signals.

Deterministic rules only. confidence = rule_constant * mean(supporting evidence confidence).
Single-snapshot signals describe PRESENCE (e.g. careers page present); signals
that assert CHANGE (new landing page, increase, product launch, expansion) are
only produced from snapshot diffs.
"""

from __future__ import annotations

import math
from statistics import mean

from ..domain.identity import iso, stable_id, utcnow
from ..domain.models import Change, Comparison, Entity, Signal
from ..domain.taxonomy import ChangeType, ComparisonDimension, ComparisonInterpretation, ObservationType, SignalType
from .index import RunIndex


def _count_strength(n: float, saturate: float) -> float:
    return round(min(1.0, math.log1p(max(n, 0)) / math.log1p(saturate)), 3)


class SignalEngine:
    def __init__(self, research_id: str, ix: RunIndex) -> None:
        self.rid = research_id
        self.ix = ix
        self.now = iso(utcnow())

    def _conf(self, refs: list[str], const: float) -> float:
        vals = [self.ix.evidence[r].confidence for r in refs if r in self.ix.evidence]
        return round(const * (mean(vals) if vals else 0.5), 3)

    def _sig(
        self,
        t: SignalType,
        subject: str,
        strength: float,
        const: float,
        summary: str,
        *,
        refs=None,
        cmp_refs=None,
        chg_refs=None,
        conf: float | None = None,
        salt: str = "",
        observed_at: str | None = None,
    ) -> Signal:
        refs = refs or []
        return Signal(
            signal_id=stable_id("sig", self.rid, t.value, subject, salt),
            type=t,
            subject_id=subject,
            observed_at=observed_at or self.now,
            strength=max(0.0, min(1.0, strength)),
            confidence=conf if conf is not None else self._conf(refs, const),
            summary=summary[:400],
            evidence_refs=refs[:8],
            comparison_refs=(cmp_refs or [])[:8],
            change_refs=(chg_refs or [])[:8],
        )

    # ----------------------------------------------------------- per entity
    def entity_signals(self, ent: Entity) -> list[Signal]:
        ix, eid, out = self.ix, ent.entity_id, []
        meta = ix.metric(eid, ObservationType.ADS_META, "active_creative_count")
        if meta:
            out.append(
                self._sig(
                    SignalType.AD_ACTIVITY_OBSERVED,
                    eid,
                    _count_strength(meta, 20),
                    0.95,
                    f"{int(meta)} active Meta creative(s) observed for {ent.company_name}",
                    refs=ix.refs_for(eid, ObservationType.ADS_META, "active_creative_count"),
                    salt="meta",
                )
            )
        gads = ix.metric(eid, ObservationType.ADS_GOOGLE, "creative_count")
        if gads:
            out.append(
                self._sig(
                    SignalType.AD_ACTIVITY_OBSERVED,
                    eid,
                    _count_strength(gads, 30),
                    0.9,
                    f"{int(gads)} Google ad creative(s) listed for {ent.company_name}",
                    refs=ix.refs_for(eid, ObservationType.ADS_GOOGLE, "creative_count"),
                    salt="google",
                )
            )
        a60 = ix.metric(eid, ObservationType.CONTENT_INVENTORY, "articles_last_60d")
        if a60:
            out.append(
                self._sig(
                    SignalType.CONTENT_ACTIVITY_OBSERVED,
                    eid,
                    _count_strength(a60, 12),
                    0.95,
                    f"{int(a60)} article(s) published in the last 60 days by {ent.company_name}",
                    refs=ix.refs_for(eid, ObservationType.CONTENT_INVENTORY, "articles_last_60d"),
                )
            )
        pricing = ix.metric(eid, ObservationType.WEBSITE_PAGE_INVENTORY, "pricing_page_count") or 0
        prices = ix.metric(eid, ObservationType.WEBSITE_COMPANY_PROFILE, "published_price_points") or 0
        offers = ix.metric(eid, ObservationType.WEBSITE_COMPANY_PROFILE, "offer_mentions") or 0
        if pricing or prices or offers:
            parts = [
                f"{int(pricing)} pricing page(s)" if pricing else "",
                f"{int(prices)} published price point(s)" if prices else "",
                f"{int(offers)} offer mention(s)" if offers else "",
            ]
            refs = ix.refs_for(eid, ObservationType.WEBSITE_PAGE_INVENTORY, "pricing_page_count", 2) + ix.refs_for(
                eid, ObservationType.WEBSITE_COMPANY_PROFILE, None, 2
            )
            out.append(
                self._sig(
                    SignalType.COMMERCIAL_INTENT,
                    eid,
                    min(1.0, 0.3 + 0.2 * sum(1 for p in parts if p)),
                    0.85,
                    f"Commercial intent on {ent.domain}: " + ", ".join(p for p in parts if p),
                    refs=refs,
                )
            )
        careers = ix.metric(eid, ObservationType.WEBSITE_PAGE_INVENTORY, "careers_page_count")
        if careers:
            out.append(
                self._sig(
                    SignalType.HIRING,
                    eid,
                    0.35,
                    0.7,
                    f"Careers page present on {ent.domain} (hiring capability observed; open-role volume unknown)",
                    refs=ix.refs_for(eid, ObservationType.WEBSITE_PAGE_INVENTORY, "careers_page_count"),
                )
            )
        posts = ix.metric(eid, ObservationType.SOCIAL_TWITTER, "posts_last_30d")
        if posts:
            out.append(
                self._sig(
                    SignalType.SOCIAL_ACTIVITY,
                    eid,
                    _count_strength(posts, 30),
                    0.95,
                    f"{int(posts)} X post(s) in the last 30 days by {ent.company_name}",
                    refs=ix.refs_for(eid, ObservationType.SOCIAL_TWITTER, "posts_last_30d"),
                )
            )
        launches = ix.metric(eid, ObservationType.SOCIAL_TWITTER, "product_launch_posts_last_30d")
        if launches:
            out.append(
                self._sig(
                    SignalType.PRODUCT_LAUNCH,
                    eid,
                    min(1.0, 0.4 + 0.15 * launches),
                    0.7,
                    f"{int(launches)} X post(s) in the last 30 days by {ent.company_name} read as product launches (rule-based label)",
                    refs=ix.refs_for(eid, ObservationType.SOCIAL_TWITTER, None),
                    salt="x_launch",
                )
            )
        return out

    # ------------------------------------------------------------ changes
    def change_signals(self, changes: list[Change]) -> list[Signal]:
        out: list[Signal] = []
        page_new: dict[tuple[str, str], list[Change]] = {}
        for ch in changes:
            if ch.type is ChangeType.NEW_PAGE and isinstance(ch.after, dict):
                page_new.setdefault((ch.entity_id, ch.after.get("kind", "other")), []).append(ch)
        for (eid, kind), chs in page_new.items():
            ids = [c.change_id for c in chs]
            refs = [r for c in chs for r in c.evidence_refs]
            urls = ", ".join(c.after["url"] for c in chs[:3])
            if kind in ("landing", "pricing", "service"):
                out.append(
                    self._sig(
                        SignalType.NEW_LANDING_PAGE,
                        eid,
                        min(1.0, 0.4 + 0.15 * len(chs)),
                        0.9,
                        f"{len(chs)} new {kind} page(s): {urls}",
                        refs=refs,
                        chg_refs=ids,
                        salt=kind,
                    )
                )
            elif kind == "product":
                out.append(
                    self._sig(
                        SignalType.PRODUCT_LAUNCH,
                        eid,
                        min(1.0, 0.4 + 0.15 * len(chs)),
                        0.75,
                        f"{len(chs)} new product page(s): {urls}",
                        refs=refs,
                        chg_refs=ids,
                    )
                )
            elif kind == "location":
                out.append(
                    self._sig(
                        SignalType.EXPANSION,
                        eid,
                        min(1.0, 0.5 + 0.15 * len(chs)),
                        0.75,
                        f"{len(chs)} new location page(s): {urls}",
                        refs=refs,
                        chg_refs=ids,
                    )
                )
            elif kind == "careers":
                out.append(
                    self._sig(
                        SignalType.HIRING,
                        eid,
                        0.6,
                        0.8,
                        f"New careers page(s): {urls}",
                        refs=refs,
                        chg_refs=ids,
                        salt="new_careers",
                    )
                )
        mapping = {
            ChangeType.NEW_OFFER: (SignalType.NEW_OFFER, 0.6),
            ChangeType.NEW_ARTICLE: (SignalType.NEW_ARTICLE, 0.5),
            ChangeType.NEW_AD_CREATIVE: (SignalType.NEW_AD_CREATIVE, 0.6),
            ChangeType.AD_ACTIVITY_INCREASED: (SignalType.AD_ACTIVITY_INCREASE, 0.75),
            ChangeType.AD_ACTIVITY_DECREASED: (SignalType.AD_ACTIVITY_DECREASE, 0.6),
            ChangeType.CONTENT_ACTIVITY_INCREASED: (SignalType.CONTENT_ACTIVITY_INCREASE, 0.7),
            ChangeType.REMOVED_PAGE: (SignalType.REMOVED_PAGE, 0.3),
            ChangeType.NEW_COMPETITOR: (SignalType.MARKET_CHANGE, 0.5),
            ChangeType.CTA_CHANGED: (SignalType.MESSAGING_CHANGE, 0.45),
            ChangeType.SERVICES_CHANGED: (SignalType.MESSAGING_CHANGE, 0.55),
            ChangeType.SOCIAL_ACTIVITY_INCREASED: (SignalType.SOCIAL_ACTIVITY_INCREASE, 0.65),
        }
        for ch in changes:
            if ch.type in mapping:
                st, strength = mapping[ch.type]
                out.append(
                    self._sig(
                        st,
                        ch.entity_id,
                        strength,
                        1.0,
                        ch.detail,
                        refs=ch.evidence_refs,
                        chg_refs=[ch.change_id],
                        conf=round(ch.confidence * 0.95, 3),
                        salt=ch.change_id,
                    )
                )
        return out

    # -------------------------------------------------------- comparisons
    def gap_signals(self, prospect: Entity, comparisons: list[Comparison], names: dict[str, str]) -> list[Signal]:
        out: list[Signal] = []
        gap_dims = {
            ComparisonDimension.META_CREATIVE_ACTIVITY: SignalType.ADVERTISING_GAP,
            ComparisonDimension.GOOGLE_AD_ACTIVITY: SignalType.ADVERTISING_GAP,
            ComparisonDimension.CONTENT_PRODUCTION_60D: SignalType.CONTENT_GAP,
        }
        more = [c for c in comparisons if c.interpretation is ComparisonInterpretation.COMPETITOR_MORE_ACTIVE]
        for c in more:
            st = gap_dims.get(c.dimension)
            if st:
                out.append(
                    self._sig(
                        st,
                        prospect.entity_id,
                        min(
                            1.0,
                            ((c.competitor_observed or 0.0) - (c.prospect_observed or 0.0))
                            / max(c.competitor_observed or 0.0, 1.0),
                        ),
                        1.0,
                        f"{c.dimension.value}: prospect {c.prospect_observed:g} vs {names.get(c.competitor_id, '?')} {c.competitor_observed:g}",
                        refs=c.evidence_refs,
                        cmp_refs=[c.comparison_id],
                        conf=c.confidence,
                        salt=c.comparison_id,
                    )
                )
        by_comp: dict[str, list[Comparison]] = {}
        for c in more:
            by_comp.setdefault(c.competitor_id, []).append(c)
        for cid, cs in by_comp.items():
            if len(cs) >= 2:
                out.append(
                    self._sig(
                        SignalType.COMPETITOR_PRESSURE,
                        cid,
                        min(1.0, 0.3 + 0.15 * len(cs)),
                        1.0,
                        f"{names.get(cid, cid)} is more active than the prospect on {len(cs)} dimension(s): "
                        + ", ".join(c.dimension.value for c in cs),
                        refs=[r for c in cs for r in c.evidence_refs],
                        cmp_refs=[c.comparison_id for c in cs],
                        conf=round(min(c.confidence for c in cs), 3),
                    )
                )
        return out
