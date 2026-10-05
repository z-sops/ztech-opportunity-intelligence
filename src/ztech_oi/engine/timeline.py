"""Opportunity Timeline (spec #23). Only events this engine owns.

Observed dated facts (article published, ad started) are placed at their real
date; engine events at research time. ZTech lifecycle events (pitch approved,
email sent...) are never produced here.
"""

from __future__ import annotations

from ..domain.identity import stable_id
from ..domain.models import (
    Change,
    Competitor,
    Entity,
    Observation,
    Opportunity,
    OpportunityScore,
    ProviderResult,
    Signal,
    TimelineEvent,
)
from ..domain.taxonomy import ChangeType, ClaimKind, ObservationType, ProviderStatus, SignalType, TimelineEventType

MAX_DATED_PER_ENTITY = 10


def build_timeline(
    *,
    research_id: str,
    started_at: str,
    completed_at: str,
    snapshot_id: str,
    prospect: Entity,
    competitors: list[Competitor],
    observations: list[Observation],
    results: list[ProviderResult],
    signals: list[Signal],
    changes: list[Change],
    opportunities: list[Opportunity],
    score: OpportunityScore,
    partial: bool,
) -> list[TimelineEvent]:
    ev: list[TimelineEvent] = []
    names = {prospect.entity_id: prospect.company_name, **{c.entity_id: c.company_name for c in competitors}}

    def add(
        t: TimelineEventType,
        eid: str,
        at: str,
        title: str,
        summary: str,
        refs=None,
        kind: ClaimKind = ClaimKind.FACT,
        salt: str = "",
    ) -> None:
        ev.append(
            TimelineEvent(
                event_id=stable_id("evt", research_id, t.value, eid, salt),
                event_type=t,
                entity_id=eid,
                occurred_at=at,
                title=title[:160],
                summary=summary[:400],
                claim_kind=kind,
                research_id=research_id,
                evidence_refs=(refs or [])[:6],
            )
        )

    add(
        TimelineEventType.RESEARCH_STARTED,
        prospect.entity_id,
        started_at,
        "Research started",
        f"Opportunity research started for {prospect.company_name}",
    )
    if prospect.domain:
        add(
            TimelineEventType.COMPANY_RESOLVED,
            prospect.entity_id,
            started_at,
            "Company resolved",
            f"{prospect.company_name} resolved to {prospect.domain}",
        )
    for c in competitors:
        add(
            TimelineEventType.COMPETITOR_DISCOVERED,
            c.entity_id,
            started_at,
            f"Competitor: {c.company_name}",
            f"{c.relationship_type.value} ({c.relationship_confidence:.2f}) — {c.reason}",
            c.evidence_refs,
            kind=ClaimKind.FACT if c.relationship_type.value == "user_provided" else ClaimKind.INFERENCE,
        )
    for r in results:
        if r.status in (ProviderStatus.UNAVAILABLE, ProviderStatus.FAILED, ProviderStatus.RATE_LIMITED):
            add(
                TimelineEventType.PROVIDER_UNAVAILABLE,
                r.entity_id,
                r.captured_at,
                f"{r.provider}: {r.status.value}",
                (r.errors[0].message if r.errors else r.status.value),
                salt=r.provider,
            )

    # dated observed facts
    for o in observations:
        if o.type is ObservationType.CONTENT_INVENTORY:
            dated = [i for i in o.items if i.get("published_at")][:MAX_DATED_PER_ENTITY]
            for i in dated:
                add(
                    TimelineEventType.ARTICLE_PUBLISHED,
                    o.entity_id,
                    i["published_at"],
                    f"Article published by {names.get(o.entity_id, '?')}",
                    (i.get("title") or i["url"])[:200],
                    salt=i["url"],
                )
        elif o.type is ObservationType.ADS_META:
            for i in [i for i in o.items if i.get("started_at")][:MAX_DATED_PER_ENTITY]:
                add(
                    TimelineEventType.META_AD_STARTED,
                    o.entity_id,
                    i["started_at"],
                    f"Meta ad started ({names.get(o.entity_id, '?')})",
                    (i.get("body") or i.get("link_title") or "")[:200],
                    salt=str(i.get("ad_id")),
                )
        elif o.type is ObservationType.ADS_GOOGLE:
            for i in [i for i in o.items if i.get("first_shown")][:MAX_DATED_PER_ENTITY]:
                add(
                    TimelineEventType.GOOGLE_AD_FIRST_SHOWN,
                    o.entity_id,
                    i["first_shown"],
                    f"Google ad first shown ({names.get(o.entity_id, '?')})",
                    f"{i.get('format')} creative",
                    salt=str(i.get("creative_id")),
                )

    for ch in changes:
        if ch.type is ChangeType.PROVIDER_AVAILABILITY_CHANGED:
            continue
        t = (
            TimelineEventType.COMPETITOR_ACTIVITY_INCREASE
            if (
                ch.entity_id != prospect.entity_id
                and ch.type
                in (
                    ChangeType.AD_ACTIVITY_INCREASED,
                    ChangeType.CONTENT_ACTIVITY_INCREASED,
                    ChangeType.SOCIAL_ACTIVITY_INCREASED,
                )
            )
            else TimelineEventType.CHANGE_DETECTED
        )
        add(
            t,
            ch.entity_id,
            completed_at,
            f"{ch.type.value}: {ch.entity_name}",
            ch.detail,
            ch.evidence_refs,
            kind=ch.claim_kind,
            salt=ch.change_id,
        )
    for s in signals:
        if s.type is SignalType.CONTENT_GAP:
            add(
                TimelineEventType.CONTENT_GAP_DETECTED,
                s.subject_id,
                completed_at,
                "Content gap detected",
                s.summary,
                s.evidence_refs,
                ClaimKind.INFERENCE,
                s.signal_id,
            )
        elif s.type is SignalType.ADVERTISING_GAP:
            add(
                TimelineEventType.ADVERTISING_GAP_DETECTED,
                s.subject_id,
                completed_at,
                "Advertising gap detected",
                s.summary,
                s.evidence_refs,
                ClaimKind.INFERENCE,
                s.signal_id,
            )
        elif s.type in (
            SignalType.COMMERCIAL_INTENT,
            SignalType.HIRING,
            SignalType.NEW_LANDING_PAGE,
            SignalType.PRODUCT_LAUNCH,
            SignalType.EXPANSION,
            SignalType.COMPETITOR_PRESSURE,
        ):
            add(
                TimelineEventType.SIGNAL_DETECTED,
                s.subject_id,
                completed_at,
                f"Signal: {s.type.value}",
                s.summary,
                s.evidence_refs,
                ClaimKind.INFERENCE,
                s.signal_id,
            )
    for opp in opportunities:
        add(
            TimelineEventType.OPPORTUNITY_IDENTIFIED,
            prospect.entity_id,
            completed_at,
            opp.title,
            f"{opp.type.value} ({opp.severity.value}, confidence {opp.confidence:.2f})",
            opp.evidence_refs,
            ClaimKind.INFERENCE,
            opp.opportunity_id,
        )
    if score.previous_score is not None and score.previous_score != score.score:
        add(
            TimelineEventType.OPPORTUNITY_SCORE_CHANGED,
            prospect.entity_id,
            completed_at,
            "Opportunity score changed",
            f"{score.previous_score} -> {score.score}",
            kind=ClaimKind.INFERENCE,
        )
    add(TimelineEventType.SNAPSHOT_CAPTURED, prospect.entity_id, completed_at, "Snapshot captured", snapshot_id)
    add(
        TimelineEventType.RESEARCH_PARTIAL if partial else TimelineEventType.RESEARCH_COMPLETED,
        prospect.entity_id,
        completed_at,
        "Research partial" if partial else "Research completed",
        "Some providers were unavailable; see limitations." if partial else "All configured providers completed.",
    )
    ev.sort(key=lambda e: (e.occurred_at, e.event_type.value))
    return ev
