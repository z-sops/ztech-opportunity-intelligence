"""Application Service Layer (spec #6) — the ONLY entry point for adapters.

    Prospect -> website/content/ads/social (prospect)  ┐
             -> competitor discovery                   ┘ (concurrently)
             -> website/content/ads/social (each competitor, bounded concurrency)
             -> evidence (+conflicts) -> comparisons -> snapshot diff
             -> signals -> opportunities -> score -> sales angles -> timeline
             -> canonical IntelligenceReport (persisted)

MCP / REST / CLI adapters call these methods and contain no business logic.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from pydantic import ValidationError

from ..domain.errors import EngineError, NotFound, ValidationFailed
from ..domain.identity import (
    entity_id,
    entity_key,
    freshness_of,
    iso,
    run_id,
    utcnow,
)
from ..domain.models import (
    ChannelSummary,
    CompanyProfile,
    CompetitorHint,
    Entity,
    Evidence,
    IntelligenceReport,
    Observation,
    Prospect,
    ProspectInput,
    ProviderResult,
    ReportTelemetry,
    ResearchJob,
    ResearchOptions,
    Snapshot,
    TimelineEvent,
)
from ..domain.taxonomy import (
    ErrorCode,
    FreshnessState,
    ObservationType,
    ProviderStatus,
    ResearchStatus,
)
from ..engine.conflicts import detect_conflicts
from ..engine.timeline import build_timeline
from ..persistence.repository import Repository
from ..providers.base import IntelligenceProvider, ResearchRequest, run_provider
from .competitor_service import CompetitorService
from .opportunity_service import Analysis, OpportunityService

# A RUNNING job older than this at startup belonged to a process that is gone.
INTERRUPTED_AFTER_S = 15 * 60

log = logging.getLogger("ztech_oi.service")

CHANNEL_GROUPS = {
    "advertising_intelligence": {"meta": "meta_ads", "google": "google_ads"},
    "content_intelligence": {"website": "website", "content": "content"},
    "social_intelligence": {"linkedin": "linkedin", "twitter": "twitter"},
}
FIRST_STAGE = ("website",)
SECOND_STAGE = ("content",)


class ResearchService:
    def __init__(
        self,
        repo: Repository,
        entity_providers: list[IntelligenceProvider],
        discovery_provider: IntelligenceProvider,
        *,
        provider_timeout_s: float = 60.0,
        entity_concurrency: int = 3,
        config_description: dict[str, Any] | None = None,
    ) -> None:
        self.repo = repo
        self.entity_providers = {p.name: p for p in entity_providers}
        self.discovery = discovery_provider
        self.competitors = CompetitorService(discovery_provider, provider_timeout_s=provider_timeout_s)
        self.analysis = OpportunityService()
        self.provider_timeout_s = provider_timeout_s
        self._entity_sem = asyncio.Semaphore(entity_concurrency)
        self.config_description = config_description or {}

    # ================================================================ public
    async def analyze_prospect(
        self, payload: dict[str, Any] | ProspectInput, options: dict[str, Any] | ResearchOptions | None = None
    ) -> IntelligenceReport:
        pinput, opts = self._validate(payload, options)
        return await self._run(pinput, opts)

    async def research_company(self, payload: dict[str, Any] | ProspectInput) -> IntelligenceReport:
        pinput, _ = self._validate(payload, None)
        pinput = pinput.model_copy(update={"known_competitors": []})
        return await self._run(pinput, ResearchOptions(max_competitors=0, discover_competitors=False))

    async def discover_competitors(self, payload: dict[str, Any] | ProspectInput, max_competitors: int = 5) -> dict[str, Any]:
        pinput, _ = self._validate(payload, None)
        rid = run_id("disc")
        prospect = self._prospect(pinput)
        result = await self.competitors.discover(rid, prospect, pinput)
        comps = self.competitors.competitors_from(result, max(0, min(max_competitors, 10)))
        return {
            "prospect": prospect.model_dump(mode="json", include={"entity_id", "company_name", "domain"}),
            "status": result.status.value,
            "competitors": [c.model_dump(mode="json") for c in comps],
            "evidence": [e.model_dump(mode="json") for e in result.evidence],
            "limitations": result.limitations,
            "errors": [e.model_dump(mode="json") for e in result.errors],
        }

    async def research_competitors(
        self, payload: dict[str, Any] | ProspectInput, competitors: list[dict[str, Any]]
    ) -> IntelligenceReport:
        try:
            hints = [CompetitorHint.model_validate(c) for c in competitors][:5]
        except ValidationError as e:
            raise ValidationFailed("invalid competitor list", {"errors": _errs(e)}) from e
        if not hints:
            raise ValidationFailed("at least one competitor is required")
        pinput, _ = self._validate(payload, None)
        pinput = pinput.model_copy(update={"known_competitors": hints})
        return await self._run(pinput, ResearchOptions(max_competitors=len(hints), discover_competitors=False))

    def get_report(self, research_id: str) -> IntelligenceReport:
        report = self.repo.get_report(research_id)
        if report is None:
            raise NotFound(f"no report with research_id '{research_id}'")
        return _refresh_freshness(report)

    def analyze_opportunity(self, research_id: str) -> dict[str, Any]:
        """Re-derive analysis from stored observations (no network), with current freshness."""
        report = self.get_report(research_id)
        prev_snap = self.repo.latest_snapshot_before(report.prospect.entity_key, report.generated_at)
        cur_snap = self.repo.get_snapshot(report.snapshot_id)
        out = self.analysis.analyse(
            research_id=report.research_id,
            prospect=report.prospect,
            competitors=report.competitors,
            observations=report.observations,
            evidence=report.evidence,
            provider_status=report.provider_status,
            provider_errors={(t.entity_id, t.provider): t.error_code for t in report.telemetry.providers},
            prev_snap=prev_snap,
            cur_snap=cur_snap,
            previous_score=report.opportunity_score.previous_score,
        )
        stale = [e.evidence_id for e in report.evidence if e.freshness is not FreshnessState.FRESH]
        return {
            "research_id": research_id,
            "analysed_at": iso(utcnow()),
            "comparisons": [c.model_dump(mode="json") for c in out.comparisons],
            "opportunities": [o.model_dump(mode="json") for o in out.opportunities],
            "opportunity_score": out.score.model_dump(mode="json"),
            "sales_angles": [a.model_dump(mode="json") for a in out.angles],
            "stale_evidence_count": len(stale),
            "limitations": ["Evidence older than 30 days is treated as expired and excluded from comparisons."] if stale else [],
        }

    def get_timeline(
        self,
        *,
        research_id: str | None = None,
        domain: str | None = None,
        company_name: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        if research_id:
            report = self.get_report(research_id)
            key = report.prospect.entity_key
        elif domain or company_name:
            key = entity_key(domain=domain, company_name=company_name or "", location=location)
        else:
            raise ValidationFailed("provide research_id, domain or company_name")
        reports = self.repo.list_reports(key)
        if not reports:
            raise NotFound("no research found for this prospect")
        events: dict[str, TimelineEvent] = {}
        for r in reports:
            for e in r.timeline:
                events.setdefault(e.event_id, e)
        dated: dict[tuple[str, str, str, str], TimelineEvent] = {}
        for e in events.values():  # same dated fact observed in several runs -> keep once
            k = (e.event_type.value, e.entity_id, e.occurred_at, e.summary)
            dated.setdefault(k, e)
        merged = sorted(dated.values(), key=lambda e: (e.occurred_at, e.event_type.value))
        return {
            "prospect_entity_key": key,
            "research_ids": [r.research_id for r in reports],
            "score_history": [
                {"research_id": r.research_id, "generated_at": r.generated_at, "score": r.opportunity_score.score}
                for r in reports
            ],
            "events": [e.model_dump(mode="json") for e in merged],
        }

    def engine_info(self) -> dict[str, Any]:
        from ..domain.taxonomy import CONFIDENCE_PRODUCERS

        return {
            "engine": "ztech-opportunity-intelligence",
            "schema_version": "1.0",
            "providers": sorted([*self.entity_providers, self.discovery.name]),
            "configuration": self.config_description,
            "confidence_producers": CONFIDENCE_PRODUCERS,
        }

    # ============================================================= pipeline
    def _validate(self, payload: Any, options: Any) -> tuple[ProspectInput, ResearchOptions]:
        try:
            pinput = payload if isinstance(payload, ProspectInput) else ProspectInput.model_validate(payload or {})
            opts = options if isinstance(options, ResearchOptions) else ResearchOptions.model_validate(options or {})
        except ValidationError as e:
            errs = _errs(e)
            code = ErrorCode.INVALID_DOMAIN if any("INVALID_DOMAIN" in x["msg"] for x in errs) else ErrorCode.VALIDATION_ERROR
            raise EngineError(code, "invalid research request", details={"errors": errs}) from e
        if opts.providers:
            unknown = set(opts.providers) - set(self.entity_providers) - {self.discovery.name}
            if unknown:
                raise ValidationFailed(f"unknown providers: {sorted(unknown)}")
        return pinput, opts

    def recover_interrupted_jobs(self, older_than_s: int = INTERRUPTED_AFTER_S) -> int:
        """At startup: a job still RUNNING from a previous process can never finish. Mark it
        FAILED ('interrupted') so a repeated idempotency key gets a terminal answer instead of
        IN_PROGRESS forever. Reports are untouched; jobs newer than the window are left alone."""
        now = utcnow()
        n = self.repo.fail_interrupted_jobs(iso(now - timedelta(seconds=older_than_s)), iso(now))
        if n:
            log.info("interrupted research jobs closed", extra={"count": n})
        return n

    def _prospect(self, pinput: ProspectInput) -> Prospect:
        key = entity_key(domain=pinput.domain, company_name=pinput.company_name, location=pinput.location)
        return Prospect(
            entity_id=entity_id(key),
            entity_key=key,
            company_name=pinput.company_name,
            domain=pinput.domain,
            location=pinput.location,
            industry=pinput.industry,
            input=pinput,
        )

    async def _collect_entity(
        self, rid: str, ent: Entity, prospect: Entity, pinput: ProspectInput, allowed: set[str] | None
    ) -> list[ProviderResult]:
        async with self._entity_sem:
            ctx: dict[str, Any] = {}
            req = ResearchRequest(
                research_id=rid,
                entity=ent,
                prospect=prospect,
                is_prospect=ent.entity_id == prospect.entity_id,
                context=ctx,
                products_services=pinput.products_services,
            )
            names = [n for n in self.entity_providers if allowed is None or n in allowed]
            results: list[ProviderResult] = []
            for stage in (FIRST_STAGE, SECOND_STAGE):
                for n in [x for x in names if x in stage]:
                    results.append(await run_provider(self.entity_providers[n], req, self.provider_timeout_s))
            rest = [n for n in names if n not in FIRST_STAGE + SECOND_STAGE]
            results += await asyncio.gather(*(run_provider(self.entity_providers[n], req, self.provider_timeout_s) for n in rest))
            return results

    def _idempotent_outcome(self, key: str, prospect_key: str) -> IntelligenceReport | None:
        """The stored outcome of one caller intent, or None when the key is unseen.

        A repeated key NEVER runs providers again. RUNNING -> IN_PROGRESS (retryable),
        FAILED -> the stored failure (terminal), finished -> the stored report, and a key
        reused for another prospect -> IDEMPOTENCY_CONFLICT.
        """
        job = self.repo.find_job_by_idempotency_key(key)
        if job is None:
            return None
        ref = {"research_id": job.research_id, "idempotent_replay": True}
        if job.prospect_entity_key != prospect_key:
            raise EngineError(
                ErrorCode.IDEMPOTENCY_CONFLICT, "this idempotency_key was already used for a different prospect"
            )
        if job.status is ResearchStatus.RUNNING:
            raise EngineError(
                ErrorCode.IN_PROGRESS, "research for this idempotency_key is still running", retryable=True, details=ref
            )
        if job.status is ResearchStatus.FAILED:
            err = job.error or {}
            try:
                code = ErrorCode(err.get("error"))
            except ValueError:
                code = ErrorCode.INTERNAL_ERROR
            if code in (ErrorCode.IN_PROGRESS, ErrorCode.IDEMPOTENCY_CONFLICT):
                code = ErrorCode.INTERNAL_ERROR
            raise EngineError(code, str(err.get("message") or "research failed"), retryable=False, details=ref)
        existing = self.repo.get_report(job.research_id)
        if existing is None:
            raise EngineError(ErrorCode.NOT_FOUND, "the report for this idempotency_key is no longer stored", details=ref)
        return _refresh_freshness(existing)

    async def _run(self, pinput: ProspectInput, opts: ResearchOptions) -> IntelligenceReport:
        prospect = self._prospect(pinput)
        if opts.idempotency_key:
            replay = self._idempotent_outcome(opts.idempotency_key, prospect.entity_key)
            if replay is not None:
                return replay
        started = utcnow()
        rid = run_id("res")
        job = ResearchJob(
            research_id=rid,
            prospect_entity_key=prospect.entity_key,
            status=ResearchStatus.RUNNING,
            request={"input": pinput.model_dump(mode="json"), "options": opts.model_dump(mode="json")},
            idempotency_key=opts.idempotency_key,
            created_at=iso(started),
        )
        if not self.repo.claim_job(job):
            # Another request claimed this key between the lookup and here.
            replay = self._idempotent_outcome(opts.idempotency_key, prospect.entity_key) if opts.idempotency_key else None
            if replay is not None:
                return replay
            raise EngineError(ErrorCode.IN_PROGRESS, "research for this idempotency_key is still running", retryable=True)
        log.info("research started", extra={"research_id": rid, "prospect": prospect.entity_key})
        try:
            report = await self._pipeline(rid, started, prospect, pinput, opts)
        except Exception as e:
            job.status, job.completed_at = ResearchStatus.FAILED, iso(utcnow())
            job.error = (
                e.to_dict()
                if isinstance(e, EngineError)
                else {"error": ErrorCode.INTERNAL_ERROR.value, "message": type(e).__name__}
            )
            self.repo.save_job(job)
            log.exception("research failed", extra={"research_id": rid})
            raise
        job.status, job.completed_at = report.status, report.generated_at
        self.repo.save_job(job)
        log.info(
            "research finished",
            extra={"research_id": rid, "status": report.status.value, "score": report.opportunity_score.score},
        )
        return report

    async def _pipeline(
        self, rid: str, started, prospect: Prospect, pinput: ProspectInput, opts: ResearchOptions
    ) -> IntelligenceReport:
        allowed = set(opts.providers) if opts.providers else None
        want_discovery = (opts.max_competitors > 0) and (allowed is None or self.discovery.name in allowed)
        hints_only = not opts.discover_competitors

        prospect_task = self._collect_entity(rid, prospect, prospect, pinput, allowed)
        if want_discovery:
            p_results, discovery = await asyncio.gather(
                prospect_task, self.competitors.discover(rid, prospect, pinput, hints_only=hints_only)
            )
            competitors = self.competitors.competitors_from(discovery, opts.max_competitors)
        else:
            p_results, discovery, competitors = await prospect_task, None, []

        comp_results_nested = await asyncio.gather(
            *(self._collect_entity(rid, c, prospect, pinput, allowed) for c in competitors)
        )
        results: list[ProviderResult] = (
            list(p_results) + ([discovery] if discovery else []) + [r for rs in comp_results_nested for r in rs]
        )

        # evidence, observations, statuses
        evidence_by_id: dict[str, Evidence] = {}
        for r in results:
            for e in r.evidence:
                evidence_by_id.setdefault(e.evidence_id, e)
        evidence, conflicts = detect_conflicts(list(evidence_by_id.values()))
        observations: list[Observation] = [o for r in results for o in r.observations]
        provider_status: dict[str, dict[str, ProviderStatus]] = {}
        provider_errors: dict[tuple[str, str], ErrorCode | None] = {}
        for r in results:
            provider_status.setdefault(r.entity_id, {})[r.provider] = r.status
            provider_errors[(r.entity_id, r.provider)] = r.errors[0].code if r.errors else None

        # profiles
        prospect = prospect.model_copy(update={"profile": _profile(observations, prospect.entity_id)})
        competitors = [self.competitors.with_profile(c, _profile(observations, c.entity_id)) for c in competitors]

        completed = utcnow()
        snapshot = Snapshot(
            snapshot_id=run_id("snap"),
            research_id=rid,
            prospect_entity_key=prospect.entity_key,
            captured_at=iso(completed),
            entities=[Entity.model_validate(e.model_dump(include=set(Entity.model_fields))) for e in [prospect, *competitors]],
            observations=observations,
            provider_status=provider_status,
        )
        prev_snap = self.repo.latest_snapshot_before(prospect.entity_key, snapshot.captured_at)
        prev_report = self.repo.latest_report(prospect.entity_key)
        analysis: Analysis = self.analysis.analyse(
            research_id=rid,
            prospect=prospect,
            competitors=competitors,
            observations=observations,
            evidence=evidence,
            provider_status=provider_status,
            provider_errors=provider_errors,
            prev_snap=prev_snap,
            cur_snap=snapshot,
            previous_score=prev_report.opportunity_score.score if prev_report else None,
        )

        # status + limitations
        limitations: list[str] = []
        degraded: list[str] = []
        names = {prospect.entity_id: prospect.company_name, **{c.entity_id: c.company_name for c in competitors}}
        for r in results:
            limitations += [f"[{r.provider}] {x}" for x in r.limitations]
            code = r.errors[0].code if r.errors else None
            if r.status in (ProviderStatus.FAILED, ProviderStatus.RATE_LIMITED, ProviderStatus.PARTIAL) or (
                r.status is ProviderStatus.UNAVAILABLE and code is not ErrorCode.PROVIDER_NOT_CONFIGURED
            ):
                degraded.append(
                    f"{r.provider}@{names.get(r.entity_id, r.entity_id)}={r.status.value}{'/' + code.value if code else ''}"
                )
        prospect_obs = [
            o for o in observations if o.entity_id == prospect.entity_id and o.type is not ObservationType.COMPETITOR_CANDIDATES
        ]
        if not prospect_obs:
            status = ResearchStatus.FAILED
            limitations.insert(
                0,
                f"{ErrorCode.COMPANY_NOT_FOUND.value}: no provider could observe the prospect; "
                "the report contains no prospect intelligence.",
            )
        elif degraded:
            status = ResearchStatus.PARTIAL
            limitations.insert(
                0,
                f"{ErrorCode.PARTIAL_RESEARCH.value}: {len(degraded)} of {len(results)} provider run(s) degraded — "
                + ", ".join(degraded[:12]),
            )
        else:
            status = ResearchStatus.COMPLETED
        if want_discovery and not competitors:
            limitations.append(f"{ErrorCode.INSUFFICIENT_EVIDENCE.value}: no competitors identified; comparisons are empty.")
        if prev_snap is None:
            limitations.append("First snapshot for this prospect: change detection, momentum and timing need a later re-run.")

        timeline = build_timeline(
            research_id=rid,
            started_at=iso(started),
            completed_at=iso(completed),
            snapshot_id=snapshot.snapshot_id,
            prospect=prospect,
            competitors=competitors,
            observations=observations,
            results=results,
            signals=analysis.signals,
            changes=analysis.changes,
            opportunities=analysis.opportunities,
            score=analysis.score,
            partial=status is not ResearchStatus.COMPLETED,
        )
        sections = _channel_sections(results)
        report = IntelligenceReport(
            research_id=rid,
            status=status,
            generated_at=iso(completed),
            snapshot_id=snapshot.snapshot_id,
            previous_snapshot_id=prev_snap.snapshot_id if prev_snap else None,
            prospect=prospect,
            competitors=competitors,
            evidence=evidence,
            observations=observations,
            signals=analysis.signals,
            advertising_intelligence=sections["advertising_intelligence"],
            content_intelligence=sections["content_intelligence"],
            social_intelligence=sections["social_intelligence"],
            comparisons=analysis.comparisons,
            changes=analysis.changes,
            opportunities=analysis.opportunities,
            opportunity_score=analysis.score,
            sales_angles=analysis.angles,
            timeline=timeline,
            conflicts=conflicts,
            limitations=list(dict.fromkeys(limitations)),
            provider_status=provider_status,
            telemetry=ReportTelemetry(
                started_at=iso(started),
                completed_at=iso(completed),
                duration_ms=int((completed - started).total_seconds() * 1000),
                providers=[r.telemetry for r in results],
            ),
        )
        # persist (idempotent upserts for evidence/entities; snapshots append-only)
        self.repo.upsert_entity(prospect)
        for c in competitors:
            self.repo.upsert_entity(c)
        self.repo.save_competitors(prospect.entity_id, rid, competitors)
        self.repo.upsert_evidence(evidence)
        self.repo.save_snapshot(snapshot)
        self.repo.save_signals(rid, report.signals)
        self.repo.save_opportunities(rid, report.opportunities)
        self.repo.save_report(report)
        return report


# ------------------------------------------------------------------ helpers
def _errs(e: ValidationError) -> list[dict[str, str]]:
    return [{"loc": ".".join(str(x) for x in err["loc"]), "msg": str(err["msg"])} for err in e.errors()]


def _profile(observations: list[Observation], eid: str) -> CompanyProfile:
    o = next((x for x in observations if x.entity_id == eid and x.type is ObservationType.WEBSITE_COMPANY_PROFILE), None)
    if not o or not o.items:
        return CompanyProfile()
    p = o.items[0]
    return CompanyProfile(
        title=p.get("title"),
        description=p.get("description"),
        services=p.get("services", []),
        products=p.get("products", []),
        emails=p.get("emails", []),
        phones=p.get("phones", []),
        social_profiles=p.get("social_profiles", {}),
        technologies=p.get("technologies", []),
        locations=p.get("locations", []),
        ctas=p.get("ctas", []),
        offers=p.get("offers", []),
        prices=p.get("prices", []),
        business_model=p.get("business_model", []),
        field_evidence=p.get("field_evidence", {}),
    )


def _channel_sections(results: list[ProviderResult]) -> dict[str, dict[str, list[ChannelSummary]]]:
    out: dict[str, dict[str, list[ChannelSummary]]] = {}
    for section, channels in CHANNEL_GROUPS.items():
        out[section] = {}
        for label, prov in channels.items():
            rows = []
            for r in results:
                if r.provider != prov:
                    continue
                metrics: dict[str, float | int | None] = {}
                for o in r.observations:
                    metrics.update(o.metrics)
                rows.append(
                    ChannelSummary(
                        entity_id=r.entity_id,
                        provider=prov,
                        status=r.status,
                        metrics=metrics,
                        observation_refs=[o.observation_id for o in r.observations],
                        limitations=r.limitations,
                        errors=r.errors,
                    )
                )
            out[section][label] = rows
    return out


def _refresh_freshness(report: IntelligenceReport) -> IntelligenceReport:
    now = utcnow()
    ev = [e.model_copy(update={"freshness": freshness_of(e.captured_at, now)}) for e in report.evidence]
    stale = sum(1 for e in ev if e.freshness is not FreshnessState.FRESH)
    lims = list(report.limitations)
    if stale:
        note = f"{stale} evidence item(s) are stale or expired as of {iso(now)[:10]}; re-run research for current data."
        if note not in lims:
            lims.insert(0, note)
    return report.model_copy(update={"evidence": ev, "limitations": lims})
