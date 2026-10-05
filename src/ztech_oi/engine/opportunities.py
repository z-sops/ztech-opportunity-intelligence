"""Opportunity Engine (spec #21). Opportunities are INFERENCES built only from
decided comparisons and detected changes. Zero opportunities is a valid result.

Confidence: min(supporting comparison/change confidence); -0.1 when only one
competitor supports it; capped at 0.6 when an underlying provider was partial.
"""

from __future__ import annotations

from ..domain.identity import stable_id
from ..domain.models import Change, Comparison, Entity, Opportunity
from ..domain.taxonomy import (
    ChangeType,
    ComparisonDimension,
    ComparisonInterpretation,
    OpportunityType,
    ProviderStatus,
    Severity,
)
from .comparison import SPEC_BY_DIM
from .index import OBS_PROVIDER, RunIndex

GAP_RULES: list[tuple[OpportunityType, list[ComparisonDimension], str, str]] = [
    (
        OpportunityType.ADVERTISING_GAP,
        [
            ComparisonDimension.META_CREATIVE_ACTIVITY,
            ComparisonDimension.GOOGLE_AD_ACTIVITY,
            ComparisonDimension.RELEVANT_AD_CREATIVES,
        ],
        "Competitors show more observed advertising activity",
        "Lower observed ad presence can mean lower share of voice for the demand competitors are paying to capture.",
    ),
    (
        OpportunityType.CONTENT_GAP,
        [ComparisonDimension.CONTENT_PRODUCTION_60D, ComparisonDimension.RELEVANT_CONTENT_60D],
        "Competitors publish more content",
        "Competitors publishing more recent content may be capturing organic search and AI-answer visibility.",
    ),
    (
        OpportunityType.LANDING_PAGE_GAP,
        [ComparisonDimension.LANDING_PAGE_ACTIVITY, ComparisonDimension.COMMERCIAL_CONTENT],
        "Competitors maintain more commercial / landing pages",
        "More commercial pages means more conversion surfaces for specific offers and search intents.",
    ),
    (
        OpportunityType.OFFER_GAP,
        [ComparisonDimension.OFFERS],
        "Competitors present more offers / pricing signals",
        "Visible offers and pricing reduce buyer friction; fewer of them may cost the prospect conversions.",
    ),
    (
        OpportunityType.SOCIAL_GAP,
        [ComparisonDimension.SOCIAL_ACTIVITY_30D],
        "Competitors post more on X",
        "More frequent posting keeps competitors visible to the same audience.",
    ),
]


def _severity(conf: float, n_competitors: int) -> Severity:
    if conf >= 0.75 and n_competitors >= 2:
        return Severity.HIGH
    if conf >= 0.55:
        return Severity.MEDIUM
    return Severity.LOW


class OpportunityEngine:
    def __init__(self, research_id: str, ix: RunIndex) -> None:
        self.rid = research_id
        self.ix = ix

    def _partial(self, entity_ids: list[str], dims: list[ComparisonDimension]) -> bool:
        provs = {OBS_PROVIDER[SPEC_BY_DIM[d].obs_type] for d in dims}
        return any(self.ix.status(e, p) is ProviderStatus.PARTIAL for e in entity_ids for p in provs)

    def build(
        self,
        prospect: Entity,
        comparisons: list[Comparison],
        changes: list[Change],
        names: dict[str, str],
        signal_index: dict[str, list[str]],
    ) -> list[Opportunity]:
        out: list[Opportunity] = []
        found: dict[OpportunityType, Opportunity] = {}
        for otype, dims, title, why in GAP_RULES:
            comps = [
                c
                for c in comparisons
                if c.dimension in dims and c.interpretation is ComparisonInterpretation.COMPETITOR_MORE_ACTIVE
            ]
            if not comps:
                continue
            competitors = sorted({c.competitor_id for c in comps})
            conf = min(c.confidence for c in comps) - (0.1 if len(competitors) == 1 else 0.0)
            limitations = []
            if self._partial([prospect.entity_id, *competitors], dims):
                conf = min(conf, 0.6)
                limitations.append("At least one underlying provider returned partial data; confidence capped at 0.6.")
            # topic-scoped evidence first: it is the most specific, pitch-ready observation (spec §4)
            comps.sort(key=lambda c: (c.topic is None, -(c.competitor_observed or 0)))
            topic = next((c.topic for c in comps if c.topic), None)
            observed = "; ".join(
                f"{names.get(c.competitor_id, c.competitor_id)}: {c.competitor_observed:g} {c.unit}"
                f"{' about ' + c.topic if c.topic else ''} vs prospect {c.prospect_observed:g}"
                f" ({c.dimension.value}{', ' + c.window if c.window else ''})"
                for c in comps[:6]
            )
            parity_or_unknown = [
                c
                for c in comparisons
                if c.dimension in dims and c.interpretation is not ComparisonInterpretation.COMPETITOR_MORE_ACTIVE
            ]
            if parity_or_unknown:
                limitations.append(
                    f"{len(parity_or_unknown)} other competitor comparison(s) on these dimensions were parity, "
                    "prospect-ahead or insufficient evidence."
                )
            opp = Opportunity(
                opportunity_id=stable_id("opp", self.rid, otype.value),
                type=otype,
                title=f"{title} around {topic}" if topic else title,
                confidence=round(max(0.0, conf), 3),
                severity=_severity(conf, len(competitors)),
                what_was_observed=observed,
                who=[names.get(c, c) for c in competitors],
                prospect_state=", ".join(sorted({f"{c.prospect_observed:g} {c.unit}" for c in comps})),
                why_it_matters=why,
                reasoning_summary=f"{len(competitors)} competitor(s) exceed the prospect beyond the minimum delta on "
                f"{', '.join(sorted({c.dimension.value for c in comps}))}.",
                evidence_refs=list(dict.fromkeys(r for c in comps for r in c.evidence_refs))[:12],
                signal_refs=[s for c in comps for s in signal_index.get(c.comparison_id, [])][:12],
                comparison_refs=[c.comparison_id for c in comps],
                limitations=limitations,
            )
            found[otype] = opp
            out.append(opp)

        # combined visibility gap
        ad, content = found.get(OpportunityType.ADVERTISING_GAP), found.get(OpportunityType.CONTENT_GAP)
        if ad and content:
            conf = round(min(ad.confidence, content.confidence), 3)
            out.append(
                Opportunity(
                    opportunity_id=stable_id("opp", self.rid, OpportunityType.COMPETITIVE_VISIBILITY_GAP.value),
                    type=OpportunityType.COMPETITIVE_VISIBILITY_GAP,
                    title="Competitors are more visible across both advertising and content",
                    confidence=conf,
                    severity=_severity(conf, len(set(ad.who) | set(content.who))),
                    what_was_observed=f"Advertising: {ad.what_was_observed} | Content: {content.what_was_observed}",
                    who=sorted(set(ad.who) | set(content.who)),
                    prospect_state=f"ads: {ad.prospect_state}; content: {content.prospect_state}",
                    why_it_matters="Competitors are investing in paid and organic visibility at the same time.",
                    reasoning_summary="Both ADVERTISING_GAP and CONTENT_GAP were inferred in this research.",
                    evidence_refs=(ad.evidence_refs + content.evidence_refs)[:12],
                    signal_refs=(ad.signal_refs + content.signal_refs)[:12],
                    comparison_refs=ad.comparison_refs + content.comparison_refs,
                    limitations=sorted(set(ad.limitations + content.limitations)),
                )
            )

        # change-driven opportunities (require history)
        comp_momentum = [
            c
            for c in changes
            if c.entity_id != prospect.entity_id
            and c.type
            in (
                ChangeType.AD_ACTIVITY_INCREASED,
                ChangeType.CONTENT_ACTIVITY_INCREASED,
                ChangeType.NEW_AD_CREATIVE,
                ChangeType.SOCIAL_ACTIVITY_INCREASED,
            )
        ]
        if comp_momentum:
            who = sorted({c.entity_name for c in comp_momentum})
            conf = round(min(c.confidence for c in comp_momentum) * 0.85, 3)
            out.append(
                Opportunity(
                    opportunity_id=stable_id("opp", self.rid, OpportunityType.COMPETITOR_MOMENTUM.value),
                    type=OpportunityType.COMPETITOR_MOMENTUM,
                    title="Competitor activity increased since the last snapshot",
                    confidence=conf,
                    severity=_severity(conf, len(who)),
                    what_was_observed="; ".join(c.detail for c in comp_momentum[:4]),
                    who=who,
                    prospect_state="Prospect showed no equivalent increase in this window"
                    if not any(
                        c.entity_id == prospect.entity_id
                        and c.type in (ChangeType.AD_ACTIVITY_INCREASED, ChangeType.CONTENT_ACTIVITY_INCREASED)
                        for c in changes
                    )
                    else "Prospect also increased activity",
                    why_it_matters="Rising competitor activity is a timely reason to review the prospect's own visibility.",
                    reasoning_summary=f"{len(comp_momentum)} competitor change(s) between snapshots.",
                    evidence_refs=[r for c in comp_momentum for r in c.evidence_refs][:12],
                    change_refs=[c.change_id for c in comp_momentum],
                    signal_refs=[x for c in comp_momentum for x in signal_index.get(c.change_id, [])][:12],
                )
            )
        comp_offers = [c for c in changes if c.entity_id != prospect.entity_id and c.type is ChangeType.NEW_OFFER]
        prospect_offers = [c for c in changes if c.entity_id == prospect.entity_id and c.type is ChangeType.NEW_OFFER]
        if comp_offers and not prospect_offers and OpportunityType.OFFER_GAP not in found:
            conf = round(min(c.confidence for c in comp_offers) * 0.85, 3)
            out.append(
                Opportunity(
                    opportunity_id=stable_id("opp", self.rid, OpportunityType.OFFER_GAP.value, "changes"),
                    type=OpportunityType.OFFER_GAP,
                    title="Competitors launched new offers since the last snapshot",
                    confidence=conf,
                    severity=_severity(conf, len({c.entity_id for c in comp_offers})),
                    what_was_observed="; ".join(c.detail for c in comp_offers[:4]),
                    who=sorted({c.entity_name for c in comp_offers}),
                    prospect_state="No new offer observed for the prospect in the same window",
                    why_it_matters="New competitor offers can pull price-sensitive demand away.",
                    reasoning_summary="Offer changes detected for competitors but not for the prospect.",
                    evidence_refs=[r for c in comp_offers for r in c.evidence_refs][:12],
                    change_refs=[c.change_id for c in comp_offers],
                    signal_refs=[x for c in comp_offers for x in signal_index.get(c.change_id, [])][:12],
                )
            )
        p_changes = [
            c
            for c in changes
            if c.entity_id == prospect.entity_id
            and c.type
            in (
                ChangeType.NEW_PAGE,
                ChangeType.NEW_OFFER,
                ChangeType.CONTENT_ACTIVITY_INCREASED,
                ChangeType.AD_ACTIVITY_INCREASED,
                ChangeType.CTA_CHANGED,
                ChangeType.SERVICES_CHANGED,
            )
        ]
        if len(p_changes) >= 2:
            conf = round(min(c.confidence for c in p_changes) * 0.8, 3)
            out.append(
                Opportunity(
                    opportunity_id=stable_id("opp", self.rid, OpportunityType.TIMING_WINDOW.value),
                    type=OpportunityType.TIMING_WINDOW,
                    title="The prospect is actively changing its go-to-market",
                    confidence=conf,
                    severity=_severity(conf, 1),
                    what_was_observed="; ".join(c.detail for c in p_changes[:4]),
                    who=[],
                    prospect_state=f"{len(p_changes)} prospect change(s) since the last snapshot",
                    why_it_matters="Companies mid-change are more receptive to help with the area they are changing.",
                    reasoning_summary="Multiple prospect-side changes were detected between snapshots.",
                    evidence_refs=[r for c in p_changes for r in c.evidence_refs][:12],
                    change_refs=[c.change_id for c in p_changes],
                    signal_refs=[x for c in p_changes for x in signal_index.get(c.change_id, [])][:12],
                )
            )
        return out
