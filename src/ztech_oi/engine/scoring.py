"""Explainable Opportunity Score (spec #22). Deterministic weighted model.

Components that cannot be computed from the available evidence are marked
computed=false and their weight is redistributed proportionally over the
computed components (effective_weight). Nothing is defaulted to a made-up value.
"""

from __future__ import annotations

from statistics import mean

from ..domain.models import (
    Change,
    Comparison,
    Entity,
    Evidence,
    EvidenceConflict,
    OpportunityScore,
    ProspectInput,
    ScoreComponent,
)
from ..domain.taxonomy import (
    ChangeType,
    ClaimKind,
    ComparisonInterpretation,
    ErrorCode,
    FreshnessState,
    ObservationType,
    ProviderStatus,
)
from .index import RunIndex

MODEL_VERSION = "score-1.0"
WEIGHTS: dict[str, float] = {
    "competitive_pressure": 0.22,
    "commercial_intent": 0.14,
    "prospect_activity": 0.12,
    "evidence_quality": 0.12,
    "icp_fit": 0.10,
    "recent_changes": 0.10,
    "market_timing": 0.08,
    "research_confidence": 0.07,
    "contactability": 0.05,
}


def _c(key: str, value: float | None, rationale: str, **inputs) -> ScoreComponent:
    return ScoreComponent(
        key=key,
        value=None if value is None else round(max(0.0, min(100.0, value)), 1),
        weight=WEIGHTS[key],
        effective_weight=0.0,
        computed=value is not None,
        rationale=rationale,
        inputs=inputs,
    )


def compute_score(
    *,
    ix: RunIndex,
    prospect: Entity,
    pinput: ProspectInput,
    comparisons: list[Comparison],
    changes: list[Change] | None,
    evidence: list[Evidence],
    conflicts: list[EvidenceConflict],
    provider_errors: dict[tuple[str, str], ErrorCode | None],
    previous_score: int | None,
) -> OpportunityScore:
    pid = prospect.entity_id
    comps: list[ScoreComponent] = []

    decided = [c for c in comparisons if c.interpretation is not ComparisonInterpretation.INSUFFICIENT_EVIDENCE]
    if decided:
        more = sum(c.confidence for c in decided if c.interpretation is ComparisonInterpretation.COMPETITOR_MORE_ACTIVE)
        total = sum(c.confidence for c in decided) or 1.0
        n_more = sum(1 for c in decided if c.interpretation is ComparisonInterpretation.COMPETITOR_MORE_ACTIVE)
        comps.append(
            _c(
                "competitive_pressure",
                100 * more / total,
                f"{n_more} of {len(decided)} decided comparisons show a competitor more active (confidence-weighted).",
                decided=len(decided),
                competitor_more_active=n_more,
            )
        )
    else:
        comps.append(_c("competitive_pressure", None, "No comparison had evidence on both sides."))

    if ix.available(pid, "website"):
        ind = {
            "pricing_page": (ix.metric(pid, ObservationType.WEBSITE_PAGE_INVENTORY, "pricing_page_count") or 0) > 0,
            "published_prices": (ix.metric(pid, ObservationType.WEBSITE_COMPANY_PROFILE, "published_price_points") or 0) > 0,
            "offers": (ix.metric(pid, ObservationType.WEBSITE_COMPANY_PROFILE, "offer_mentions") or 0) > 0,
            "ctas": (ix.metric(pid, ObservationType.WEBSITE_COMPANY_PROFILE, "cta_count") or 0) > 0,
            "landing_pages": (ix.metric(pid, ObservationType.WEBSITE_PAGE_INVENTORY, "landing_page_count") or 0) > 0,
        }
        comps.append(
            _c(
                "commercial_intent",
                100 * sum(ind.values()) / len(ind),
                f"{sum(ind.values())}/5 commercial indicators present on the prospect site.",
                **ind,
            )
        )
    else:
        comps.append(_c("commercial_intent", None, "Prospect website was not observable."))

    subs: dict[str, float] = {}
    a60 = ix.metric(pid, ObservationType.CONTENT_INVENTORY, "articles_last_60d")
    if a60 is not None:
        subs["content_60d"] = min(100.0, a60 / 8 * 100)
    meta = ix.metric(pid, ObservationType.ADS_META, "active_creative_count")
    if meta is not None and not (meta == 0 and ix.metric(pid, ObservationType.ADS_META, "coverage_reliable") == 0):
        subs["meta_creatives"] = min(100.0, meta / 10 * 100)
    g = ix.metric(pid, ObservationType.ADS_GOOGLE, "creative_count")
    if g is not None:
        subs["google_creatives"] = min(100.0, g / 15 * 100)
    x = ix.metric(pid, ObservationType.SOCIAL_TWITTER, "posts_last_30d")
    if x is not None:
        subs["x_posts_30d"] = min(100.0, x / 20 * 100)
    comps.append(
        _c(
            "prospect_activity",
            mean(subs.values()) if subs else None,
            f"Mean of observable prospect activity channels: {', '.join(subs) or 'none observable'}.",
            **subs,
        )
    )

    fresh_facts = [
        e for e in evidence if e.entity_id == pid and e.claim_kind is ClaimKind.FACT and e.freshness is FreshnessState.FRESH
    ]
    if fresh_facts:
        conflict_pen = min(30.0, 10.0 * sum(1 for c in conflicts if c.entity_id == pid))
        comps.append(
            _c(
                "evidence_quality",
                100 * mean(e.confidence for e in fresh_facts) - conflict_pen,
                f"{len(fresh_facts)} fresh fact(s); mean confidence; -{conflict_pen:.0f} for evidence conflicts.",
                fresh_facts=len(fresh_facts),
                conflicts=len([c for c in conflicts if c.entity_id == pid]),
            )
        )
    else:
        comps.append(_c("evidence_quality", None, "No fresh factual evidence about the prospect."))

    icp = pinput.icp
    if icp and (icp.industries or icp.locations or icp.keywords):
        prof = ix.profile(pid)
        text = " ".join(
            [
                prospect.industry or "",
                prospect.location or "",
                prof.get("description") or "",
                " ".join(prof.get("services") or []),
                " ".join(prof.get("products") or []),
                " ".join(prof.get("locations") or []),
            ]
        ).lower()
        checks = {}
        if icp.industries:
            checks["industry"] = any(i.lower() in text for i in icp.industries)
        if icp.locations:
            checks["location"] = any(loc.lower() in text for loc in icp.locations)
        if icp.keywords:
            checks["keywords"] = sum(1 for k in icp.keywords if k.lower() in text) / len(icp.keywords) >= 0.3
        comps.append(
            _c(
                "icp_fit",
                100 * sum(checks.values()) / len(checks),
                f"ICP criteria matched against observed profile text: {checks}.",
                **checks,
            )
        )
    else:
        comps.append(_c("icp_fit", None, "No ICP criteria supplied by the caller."))

    if changes is None:
        comps.append(_c("recent_changes", None, "First snapshot for this prospect: change history not yet available."))
        comps.append(_c("market_timing", None, "First snapshot for this prospect: market movement not yet observable."))
    else:
        mine = [
            c
            for c in changes
            if c.entity_id == pid and c.type not in (ChangeType.PROVIDER_AVAILABILITY_CHANGED, ChangeType.PAGE_INVENTORY_CHANGED)
        ]
        comps.append(
            _c(
                "recent_changes",
                min(100.0, len(mine) / 5 * 100),
                f"{len(mine)} prospect change(s) since the previous snapshot.",
                prospect_changes=len(mine),
            )
        )
        theirs = [
            c
            for c in changes
            if c.entity_id != pid
            and c.type
            in (
                ChangeType.AD_ACTIVITY_INCREASED,
                ChangeType.CONTENT_ACTIVITY_INCREASED,
                ChangeType.NEW_AD_CREATIVE,
                ChangeType.NEW_OFFER,
                ChangeType.NEW_COMPETITOR,
                ChangeType.NEW_PAGE,
                ChangeType.SERVICES_CHANGED,
                ChangeType.SOCIAL_ACTIVITY_INCREASED,
            )
        ]
        comps.append(
            _c(
                "market_timing",
                min(100.0, len(theirs) / 6 * 100),
                f"{len(theirs)} competitor/market change(s) since the previous snapshot.",
                market_changes=len(theirs),
            )
        )

    statuses = {(eid, prov): st for eid, m in ix.provider_status.items() for prov, st in m.items()}
    considered = {
        k: v
        for k, v in statuses.items()
        if v is not ProviderStatus.UNSUPPORTED and provider_errors.get(k) is not ErrorCode.PROVIDER_NOT_CONFIGURED
    }
    if considered:
        ok = sum(1.0 if v is ProviderStatus.SUCCESS else 0.5 if v is ProviderStatus.PARTIAL else 0.0 for v in considered.values())
        skipped = len(statuses) - len(considered)
        comps.append(
            _c(
                "research_confidence",
                100 * ok / len(considered),
                f"{ok:g}/{len(considered)} configured provider runs succeeded ({skipped} unsupported/not-configured runs excluded).",
                runs=len(considered),
                excluded=skipped,
            )
        )
    else:
        comps.append(_c("research_confidence", None, "No configured provider ran."))

    if ix.available(pid, "website"):
        prof = ix.profile(pid)
        pts = (
            (40 if prof.get("emails") else 0)
            + (30 if prof.get("phones") else 0)
            + (30 if (ix.metric(pid, ObservationType.WEBSITE_PAGE_INVENTORY, "contact_page_count") or 0) > 0 else 0)
        )
        comps.append(
            _c(
                "contactability",
                pts,
                "Public email (40) + phone (30) + contact page (30) on the prospect site.",
                email=bool(prof.get("emails")),
                phone=bool(prof.get("phones")),
            )
        )
    else:
        comps.append(_c("contactability", None, "Prospect website was not observable."))

    computed = [c for c in comps if c.computed]
    total_w = sum(c.weight for c in computed)
    for c in comps:
        c.effective_weight = round(c.weight / total_w, 4) if c.computed and total_w else 0.0
    score = int(round(sum((c.value or 0) * c.effective_weight for c in computed))) if computed else 0
    strongest = max(computed, key=lambda c: c.value or 0, default=None)
    weakest = min(computed, key=lambda c: c.value or 0, default=None)
    explanation = [
        f"Score {score}/100 from {len(computed)} of {len(comps)} components (model {MODEL_VERSION}).",
        f"Not computed: {', '.join(c.key for c in comps if not c.computed) or 'none'} — their weight was redistributed.",
    ]
    if strongest and weakest:
        explanation.append(f"Strongest: {strongest.key} ({strongest.value:g}); weakest: {weakest.key} ({weakest.value:g}).")
    if previous_score is not None:
        explanation.append(f"Previous score {previous_score}; change {score - previous_score:+d}.")
    explanation.append("Deterministic: identical inputs always produce the identical score.")
    return OpportunityScore(
        score=score, model_version=MODEL_VERSION, components=comps, explanation=explanation, previous_score=previous_score
    )
