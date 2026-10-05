"""Opportunity application service (spec §20-§24, §40).

Pure, deterministic analysis over already-collected observations — no network.
Used both for a fresh research and for offline re-analysis of a stored report.

  observations + evidence ─► comparisons ─► snapshot diff ─► signals
                          ─► opportunities ─► explainable score ─► sales angles
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.identity import freshness_of
from ..domain.models import (
    Change,
    Comparison,
    Competitor,
    Evidence,
    Observation,
    Opportunity,
    OpportunityScore,
    Prospect,
    SalesAngle,
    Signal,
    Snapshot,
)
from ..domain.taxonomy import ErrorCode, ProviderStatus
from ..engine.angles import generate_angles
from ..engine.comparison import ComparisonEngine
from ..engine.conflicts import detect_conflicts
from ..engine.diff import ChangeDetector
from ..engine.index import RunIndex
from ..engine.opportunities import OpportunityEngine
from ..engine.scoring import compute_score
from ..engine.signals import SignalEngine


@dataclass
class Analysis:
    comparisons: list[Comparison]
    changes: list[Change]
    signals: list[Signal]
    opportunities: list[Opportunity]
    score: OpportunityScore
    angles: list[SalesAngle]


def topic_label(prospect: Prospect) -> str | None:
    """The prospect's products/services define the topic for topic-scoped comparisons (spec §4)."""
    ps = [p.strip() for p in prospect.input.products_services if p.strip()]
    return ", ".join(ps[:3]) if ps else None


class OpportunityService:
    def analyse(
        self,
        *,
        research_id: str,
        prospect: Prospect,
        competitors: list[Competitor],
        observations: list[Observation],
        evidence: list[Evidence],
        provider_status: dict[str, dict[str, ProviderStatus]],
        provider_errors: dict[tuple[str, str], ErrorCode | None],
        prev_snap: Snapshot | None,
        cur_snap: Snapshot | None,
        previous_score: int | None,
    ) -> Analysis:
        evidence = [e.model_copy(update={"freshness": freshness_of(e.captured_at)}) for e in evidence]
        ix = RunIndex(observations, evidence, provider_status)
        names = {prospect.entity_id: prospect.company_name, **{c.entity_id: c.company_name for c in competitors}}

        comparisons = ComparisonEngine().build(research_id, ix, prospect, competitors, topic=topic_label(prospect))
        detector = ChangeDetector(topics=prospect.input.products_services)
        changes = detector.diff(prev_snap, cur_snap) if (prev_snap and cur_snap) else []

        se = SignalEngine(research_id, ix)
        signals = [s for ent in [prospect, *competitors] for s in se.entity_signals(ent)]
        change_signals = se.change_signals(changes)
        gap_signals = se.gap_signals(prospect, comparisons, names)
        signals += change_signals + gap_signals

        # index signals by the comparison / change they came from, so opportunities can cite them
        sig_index: dict[str, list[str]] = {}
        for s in [*gap_signals, *change_signals]:
            for ref in [*s.comparison_refs, *s.change_refs]:
                sig_index.setdefault(ref, []).append(s.signal_id)

        opportunities = OpportunityEngine(research_id, ix).build(prospect, comparisons, changes, names, sig_index)
        _, conflicts = detect_conflicts(evidence)
        score = compute_score(
            ix=ix,
            prospect=prospect,
            pinput=prospect.input,
            comparisons=comparisons,
            changes=changes if prev_snap else None,
            evidence=evidence,
            conflicts=conflicts,
            provider_errors=provider_errors,
            previous_score=previous_score,
        )
        angles = generate_angles(research_id, prospect.company_name, opportunities)
        return Analysis(comparisons, changes, signals, opportunities, score, angles)
