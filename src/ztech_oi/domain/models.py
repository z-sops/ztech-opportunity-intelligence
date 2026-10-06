"""Canonical domain models (spec #17-#30).

Everything that crosses the engine boundary is one of these Pydantic models,
so it is JSON-serialisable, validated and documented via JSON Schema. Models
use `extra="forbid"` so unexpected fields are rejected at the boundary.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .identity import normalize_domain
from .taxonomy import (
    ChangeType,
    ClaimKind,
    ComparisonDimension,
    ComparisonInterpretation,
    EntityKind,
    ErrorCode,
    FreshnessState,
    ObservationType,
    OpportunityType,
    ProviderStatus,
    RelationshipType,
    ResearchStatus,
    Severity,
    SignalType,
    TimelineEventType,
)

SCHEMA_VERSION: Literal["1.0"] = "1.0"

Confidence = float  # always 0..1, validated via Field(ge=0, le=1)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False, ser_json_timedelta="iso8601")


# ======================================================================= input
class CompetitorHint(_Model):
    company_name: str = Field(min_length=1, max_length=120)
    domain: str | None = Field(default=None, max_length=253)


class ICPCriteria(_Model):
    """Optional ideal-customer-profile used only for the icp_fit score component."""

    industries: list[str] = Field(default_factory=list, max_length=20)
    locations: list[str] = Field(default_factory=list, max_length=20)
    keywords: list[str] = Field(default_factory=list, max_length=30)


class ResearchOptions(_Model):
    max_competitors: int = Field(default=3, ge=0, le=5)
    discover_competitors: bool = True
    providers: list[str] | None = Field(default=None, description="Optional allow-list of provider names to run (default: all).")
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="One caller intent. A repeat returns the stored outcome and never re-runs providers.",
    )


class ProspectInput(_Model):
    company_name: str = Field(min_length=1, max_length=120)
    domain: str | None = Field(default=None, max_length=253)
    location: str | None = Field(default=None, max_length=120)
    industry: str | None = Field(default=None, max_length=120)
    products_services: list[str] = Field(default_factory=list, max_length=10)
    known_competitors: list[CompetitorHint] = Field(default_factory=list, max_length=10)
    icp: ICPCriteria | None = None

    @field_validator("company_name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("company_name must not be blank")
        return v

    @field_validator("domain")
    @classmethod
    def _check_domain(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        d = normalize_domain(v)
        if not d:
            raise ValueError("INVALID_DOMAIN: domain is not a valid public hostname")
        return d


# ===================================================================== entities
class EstimateRange(_Model):
    low: float
    high: float
    unit: str
    method: str
    source: str

    @model_validator(mode="after")
    def _order(self) -> EstimateRange:
        if self.high < self.low:
            raise ValueError("estimate high < low")
        return self


class Evidence(_Model):
    evidence_id: str
    entity_id: str
    provider: str
    source_type: str
    source_url: str | None = None
    observation_type: ObservationType
    observed_at: str | None = Field(
        default=None, description="When the underlying thing happened/was published at the source, if known."
    )
    captured_at: str
    expires_at: str | None = None
    freshness: FreshnessState = FreshnessState.FRESH
    claim_kind: ClaimKind
    claim: str = Field(max_length=600)
    metric: str | None = Field(default=None, description="Structured metric key this evidence supports.")
    value: float | int | str | bool | None = None
    estimate: EstimateRange | None = None
    raw_reference: dict[str, Any] = Field(default_factory=dict)
    content_hash: str | None = None
    confidence: float = Field(ge=0, le=1)
    conflicts_with: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _estimate_rules(self) -> Evidence:
        if self.estimate is not None and self.claim_kind is not ClaimKind.ESTIMATE:
            raise ValueError("estimate ranges must use claim_kind=estimate")
        return self


class Observation(_Model):
    observation_id: str
    entity_id: str
    provider: str
    type: ObservationType
    captured_at: str
    claim_kind: ClaimKind = ClaimKind.FACT
    metrics: dict[str, float | int | None] = Field(default_factory=dict)
    items: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class CompanyProfile(_Model):
    title: str | None = None
    description: str | None = None
    services: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    social_profiles: dict[str, str] = Field(default_factory=dict)
    technologies: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    ctas: list[str] = Field(default_factory=list)
    offers: list[str] = Field(default_factory=list)
    prices: list[str] = Field(default_factory=list)
    business_model: list[str] = Field(
        default_factory=list, description="INFERENCE: business-model labels derived from observed site signals."
    )
    field_evidence: dict[str, list[str]] = Field(default_factory=dict, description="Provenance: profile field -> evidence ids.")


class Entity(_Model):
    entity_id: str
    entity_key: str
    kind: EntityKind
    company_name: str
    domain: str | None = None
    location: str | None = None
    industry: str | None = None


class Prospect(Entity):
    kind: Literal[EntityKind.PROSPECT] = EntityKind.PROSPECT
    input: ProspectInput
    profile: CompanyProfile = Field(default_factory=CompanyProfile)


class Competitor(Entity):
    kind: Literal[EntityKind.COMPETITOR] = EntityKind.COMPETITOR
    relationship_type: RelationshipType
    relationship_confidence: float = Field(ge=0, le=1)
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)
    discovered_via: Literal["user", "search", "search+llm"]
    profile: CompanyProfile = Field(default_factory=CompanyProfile)

    @property
    def competitor_id(self) -> str:
        return self.entity_id


# =================================================================== providers
class ProviderErrorInfo(_Model):
    code: ErrorCode
    message: str


class ProviderTelemetry(_Model):
    research_id: str
    provider: str
    entity_id: str
    started_at: str
    completed_at: str
    duration_ms: int
    status: ProviderStatus
    items_observed: int = 0
    evidence_created: int = 0
    error_code: ErrorCode | None = None
    retry_count: int = 0


class ProviderResult(_Model):
    provider: str
    entity_id: str
    status: ProviderStatus
    observations: list[Observation] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    captured_at: str
    errors: list[ProviderErrorInfo] = Field(default_factory=list)
    telemetry: ProviderTelemetry


class ChannelSummary(_Model):
    """Per-entity, per-provider summary used in the report's *_intelligence sections."""

    entity_id: str
    provider: str
    status: ProviderStatus
    metrics: dict[str, float | int | None] = Field(default_factory=dict)
    observation_refs: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    errors: list[ProviderErrorInfo] = Field(default_factory=list)


# ====================================================================== engine
class Signal(_Model):
    signal_id: str
    type: SignalType
    subject_id: str
    observed_at: str
    strength: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    claim_kind: ClaimKind = ClaimKind.INFERENCE
    summary: str
    evidence_refs: list[str] = Field(default_factory=list)
    comparison_refs: list[str] = Field(default_factory=list)
    change_refs: list[str] = Field(default_factory=list)


class Comparison(_Model):
    comparison_id: str
    dimension: ComparisonDimension
    prospect_id: str
    competitor_id: str
    prospect_observed: float | None
    competitor_observed: float | None
    unit: str
    topic: str | None = Field(default=None, description="Topic scope (e.g. 'wedding cakes') for topic-scoped dimensions.")
    window: str | None = None
    interpretation: ComparisonInterpretation
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)
    note: str | None = None


class Change(_Model):
    change_id: str
    type: ChangeType
    channel: str | None = Field(default=None, description="website | content | meta_ads | google_ads | twitter")
    entity_id: str
    entity_name: str
    previous_snapshot_id: str
    current_snapshot_id: str
    before: Any = None
    after: Any = None
    detail: str
    claim_kind: ClaimKind
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)


class Opportunity(_Model):
    opportunity_id: str
    type: OpportunityType
    title: str
    confidence: float = Field(ge=0, le=1)
    severity: Severity
    claim_kind: Literal[ClaimKind.INFERENCE] = ClaimKind.INFERENCE
    what_was_observed: str
    who: list[str] = Field(default_factory=list, description="Competitor names driving the opportunity.")
    prospect_state: str
    why_it_matters: str
    reasoning_summary: str
    evidence_refs: list[str] = Field(default_factory=list)
    signal_refs: list[str] = Field(default_factory=list)
    comparison_refs: list[str] = Field(default_factory=list)
    change_refs: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ScoreComponent(_Model):
    key: str
    value: float | None = Field(default=None, ge=0, le=100)
    weight: float = Field(ge=0, le=1)
    effective_weight: float = Field(ge=0, le=1)
    computed: bool
    rationale: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class OpportunityScore(_Model):
    score: int = Field(ge=0, le=100)
    model_version: str
    components: list[ScoreComponent]
    explanation: list[str]
    previous_score: int | None = None


class TimelineEvent(_Model):
    event_id: str
    event_type: TimelineEventType
    entity_id: str
    occurred_at: str
    title: str
    summary: str
    claim_kind: ClaimKind = ClaimKind.FACT
    research_id: str
    evidence_refs: list[str] = Field(default_factory=list)


class SalesAngle(_Model):
    angle_id: str
    angle: str
    summary: str
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)
    opportunity_refs: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    do_not_claim: list[str] = Field(
        default_factory=list, description="Statements the evidence does NOT support; never put these in a pitch."
    )


class EvidenceConflict(_Model):
    entity_id: str
    metric: str
    evidence_refs: list[str]
    values: list[float | int | str | bool | None]
    resolution: str


# ===================================================================== snapshot
class Snapshot(_Model):
    snapshot_id: str
    research_id: str
    prospect_entity_key: str
    captured_at: str
    entities: list[Entity]
    observations: list[Observation]
    provider_status: dict[str, dict[str, ProviderStatus]] = Field(
        default_factory=dict, description="entity_id -> provider -> status"
    )


# ======================================================================= report
class ReportTelemetry(_Model):
    started_at: str
    completed_at: str
    duration_ms: int
    providers: list[ProviderTelemetry]


class IntelligenceReport(_Model):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    research_id: str
    status: ResearchStatus
    generated_at: str
    snapshot_id: str
    previous_snapshot_id: str | None = None
    prospect: Prospect
    competitors: list[Competitor]
    evidence: list[Evidence]
    observations: list[Observation]
    signals: list[Signal]
    advertising_intelligence: dict[str, list[ChannelSummary]]
    content_intelligence: dict[str, list[ChannelSummary]]
    social_intelligence: dict[str, list[ChannelSummary]]
    comparisons: list[Comparison]
    changes: list[Change]
    opportunities: list[Opportunity]
    opportunity_score: OpportunityScore
    sales_angles: list[SalesAngle]
    timeline: list[TimelineEvent]
    conflicts: list[EvidenceConflict]
    limitations: list[str]
    provider_status: dict[str, dict[str, ProviderStatus]]
    telemetry: ReportTelemetry


class ResearchJob(_Model):
    research_id: str
    prospect_entity_key: str
    status: ResearchStatus
    request: dict[str, Any]
    idempotency_key: str | None = None
    created_at: str
    completed_at: str | None = None
    error: dict[str, Any] | None = None
