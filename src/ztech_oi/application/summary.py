"""Compact, token-friendly views of a canonical report (for MCP/REST callers).

The full canonical report is always retrievable via get_opportunity_report;
these views only select fields — they never add or reinterpret content.
"""

from __future__ import annotations

from typing import Any

from ..domain.models import IntelligenceReport

SECTIONS = {
    "prospect",
    "competitors",
    "evidence",
    "observations",
    "signals",
    "advertising_intelligence",
    "content_intelligence",
    "social_intelligence",
    "comparisons",
    "changes",
    "opportunities",
    "opportunity_score",
    "sales_angles",
    "timeline",
    "conflicts",
    "limitations",
    "provider_status",
    "telemetry",
}
ALWAYS = {"schema_version", "research_id", "status", "generated_at", "snapshot_id", "previous_snapshot_id"}


def summarize(report: IntelligenceReport) -> dict[str, Any]:
    names = {report.prospect.entity_id: report.prospect.company_name, **{c.entity_id: c.company_name for c in report.competitors}}
    decided = [c for c in report.comparisons if c.interpretation.value != "insufficient_evidence"]
    return {
        "schema_version": report.schema_version,
        "research_id": report.research_id,
        "status": report.status.value,
        "generated_at": report.generated_at,
        "prospect": {
            "entity_id": report.prospect.entity_id,
            "company_name": report.prospect.company_name,
            "domain": report.prospect.domain,
            "profile": report.prospect.profile.model_dump(mode="json", exclude={"field_evidence"}),
        },
        "competitors": [
            {
                "entity_id": c.entity_id,
                "company_name": c.company_name,
                "domain": c.domain,
                "relationship_type": c.relationship_type.value,
                "relationship_confidence": c.relationship_confidence,
                "reason": c.reason,
            }
            for c in report.competitors
        ],
        "opportunity_score": {
            "score": report.opportunity_score.score,
            "previous_score": report.opportunity_score.previous_score,
            "explanation": report.opportunity_score.explanation,
        },
        "opportunities": [
            {
                "opportunity_id": o.opportunity_id,
                "type": o.type.value,
                "title": o.title,
                "severity": o.severity.value,
                "confidence": o.confidence,
                "what_was_observed": o.what_was_observed,
                "who": o.who,
                "evidence_refs": o.evidence_refs[:5],
            }
            for o in report.opportunities
        ],
        "sales_angles": [
            {
                "angle": a.angle,
                "summary": a.summary,
                "confidence": a.confidence,
                "do_not_claim": a.do_not_claim,
                "evidence_refs": a.evidence_refs[:5],
            }
            for a in report.sales_angles
        ],
        "key_comparisons": [
            {
                "dimension": c.dimension.value,
                "competitor": names.get(c.competitor_id),
                "prospect": c.prospect_observed,
                "competitor_value": c.competitor_observed,
                "unit": c.unit,
                "window": c.window,
                "interpretation": c.interpretation.value,
                "confidence": c.confidence,
            }
            for c in decided[:20]
        ],
        "changes_since_previous": len(report.changes),
        "provider_status": {names.get(k, k): {p: s.value for p, s in v.items()} for k, v in report.provider_status.items()},
        "counts": {
            "evidence": len(report.evidence),
            "signals": len(report.signals),
            "comparisons": len(report.comparisons),
            "timeline_events": len(report.timeline),
            "conflicts": len(report.conflicts),
        },
        "limitations": report.limitations[:25],
        "note": "Summary view. Call get_opportunity_report(research_id) for the full canonical report.",
    }


def select_sections(report: IntelligenceReport, sections: list[str] | None) -> dict[str, Any]:
    data = report.model_dump(mode="json")
    if not sections:
        return data
    unknown = set(sections) - SECTIONS
    if unknown:
        raise ValueError(f"unknown sections: {sorted(unknown)}")
    return {k: v for k, v in data.items() if k in ALWAYS or k in sections}
