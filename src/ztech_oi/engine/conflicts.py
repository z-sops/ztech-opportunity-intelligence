"""Evidence conflict detection (spec #38 'conflicting evidence').

Two or more evidence records about the same entity + metric that disagree are
NOT silently merged: all are kept, linked via `conflicts_with`, the lower-
confidence ones are down-weighted (x0.6), and an EvidenceConflict is reported.
"""

from __future__ import annotations

from collections import defaultdict

from ..domain.models import Evidence, EvidenceConflict


def detect_conflicts(evidence: list[Evidence]) -> tuple[list[Evidence], list[EvidenceConflict]]:
    groups: dict[tuple[str, str, str], list[Evidence]] = defaultdict(list)
    for e in evidence:
        if e.metric and e.value is not None:
            groups[(e.entity_id, e.observation_type.value, e.metric)].append(e)
    conflicts: list[EvidenceConflict] = []
    updated: dict[str, Evidence] = {}
    for (eid, _otype, metric), evs in groups.items():
        values = {repr(e.value) for e in evs}
        if len(evs) < 2 or len(values) < 2:
            continue
        evs = sorted(evs, key=lambda e: -e.confidence)
        winner = evs[0]
        ids = [e.evidence_id for e in evs]
        for e in evs:
            others = [i for i in ids if i != e.evidence_id]
            conf = e.confidence if e is winner else round(e.confidence * 0.6, 3)
            updated[e.evidence_id] = e.model_copy(update={"conflicts_with": others, "confidence": conf})
        conflicts.append(
            EvidenceConflict(
                entity_id=eid,
                metric=metric,
                evidence_refs=ids,
                values=[e.value for e in evs],
                resolution=f"Kept all values; highest-confidence ({winner.evidence_id}) preferred, others down-weighted x0.6.",
            )
        )
    return [updated.get(e.evidence_id, e) for e in evidence], conflicts
