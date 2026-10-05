"""Sales Angle Generator (spec #24). Evidence-backed only, deterministic templates.

No opportunity -> no angle (we do not invent a generic one). Each angle carries
`do_not_claim`: statements the evidence does NOT support, so ZTech's pitch
writer has explicit guard-rails (e.g. never quote ad spend).
"""

from __future__ import annotations

from ..domain.identity import stable_id
from ..domain.models import Opportunity, SalesAngle
from ..domain.taxonomy import OpportunityType

TEMPLATES: dict[OpportunityType, tuple[str, str]] = {
    OpportunityType.ADVERTISING_GAP: (
        "Competitive advertising pressure",
        "Observed competitors are running more ads than {p}: {obs}. This may be a chance to raise {p}'s paid visibility.",
    ),
    OpportunityType.CONTENT_GAP: (
        "Content visibility gap",
        "Competitors have published more recent content than {p}: {obs}. Consistent publishing could close this gap.",
    ),
    OpportunityType.COMPETITIVE_VISIBILITY_GAP: (
        "Competitors investing in paid + organic visibility",
        "Competitors appear more active in both advertising and content ({who}). {p}'s observed activity is lower on both.",
    ),
    OpportunityType.LANDING_PAGE_GAP: (
        "More conversion surfaces at competitors",
        "Competitors maintain more commercial/landing pages than {p}: {obs}.",
    ),
    OpportunityType.OFFER_GAP: (
        "Offer and pricing visibility",
        "Competitors present more visible offers or new offers ({who}): {obs}.",
    ),
    OpportunityType.SOCIAL_GAP: ("Social presence gap", "Competitors post more frequently on X: {obs}."),
    OpportunityType.COMPETITOR_MOMENTUM: (
        "Competitor momentum",
        "Since the last check, competitors increased activity: {obs}.",
    ),
    OpportunityType.TIMING_WINDOW: (
        "Timing: {p} is changing its go-to-market",
        "{p} recently made changes ({obs}); help is most relevant while they are in motion.",
    ),
}

ALWAYS_DO_NOT_CLAIM = [
    "Any advertising spend, budget, impressions, clicks or ROI figure (not observable).",
    "That the prospect is losing customers or revenue (not observed).",
    "Anything about LinkedIn activity (provider unsupported).",
]


def generate_angles(research_id: str, prospect_name: str, opportunities: list[Opportunity]) -> list[SalesAngle]:
    out: list[SalesAngle] = []
    for o in opportunities:
        title, body = TEMPLATES[o.type]
        dnc = list(ALWAYS_DO_NOT_CLAIM)
        if o.type in (OpportunityType.ADVERTISING_GAP, OpportunityType.COMPETITIVE_VISIBILITY_GAP):
            dnc.append("That the prospect runs no ads at all — only observed counts in the queried sources/regions are known.")
        if o.type is OpportunityType.CONTENT_GAP:
            dnc.append("That competitors rank higher in search — rankings were not measured.")
        out.append(
            SalesAngle(
                angle_id=stable_id("ang", research_id, o.opportunity_id),
                angle=title.format(p=prospect_name),
                summary=body.format(p=prospect_name, obs=o.what_was_observed[:300], who=", ".join(o.who) or "competitors")[:600],
                confidence=o.confidence,
                evidence_refs=o.evidence_refs[:8],
                opportunity_refs=[o.opportunity_id],
                limitations=[*o.limitations, "This engine does not send outreach; ZTech decides whether this becomes a pitch."],
                do_not_claim=dnc,
            )
        )
    return out
