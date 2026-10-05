"""Provider architecture (spec #26, #27).

Every external source sits behind `IntelligenceProvider`. Providers:
  * never fabricate data (spec #46)
  * emit structured `Observation`s (metrics/items) + provenance `Evidence`
  * return honest statuses: success / partial / unavailable / unsupported /
    failed / rate_limited
  * are wrapped by `run_provider`, which enforces a hard timeout and converts
    any exception into a FAILED/RATE_LIMITED/UNAVAILABLE envelope.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..domain.errors import EngineError
from ..domain.identity import (
    capture_window,
    default_expires_at,
    evidence_id,
    freshness_of,
    iso,
    observation_id,
    utcnow,
)
from ..domain.models import (
    Entity,
    EstimateRange,
    Evidence,
    Observation,
    ProviderErrorInfo,
    ProviderResult,
    ProviderTelemetry,
)
from ..domain.taxonomy import ClaimKind, ErrorCode, ObservationType, ProviderStatus

log = logging.getLogger("ztech_oi.providers")

MAX_CLAIM = 580
MAX_EXCERPT = 400


@dataclass
class ResearchRequest:
    research_id: str
    entity: Entity
    prospect: Entity
    is_prospect: bool
    #: shared per-entity scratchpad, written by earlier providers (e.g. website
    #: provider exposes sitemap entries and social handles to later providers)
    context: dict[str, Any] = field(default_factory=dict)
    products_services: list[str] = field(default_factory=list)


class IntelligenceProvider(Protocol):
    name: str
    source_type: str
    #: "per_entity" providers run for prospect and every competitor
    scope: str

    async def collect(self, request: ResearchRequest) -> ProviderResult: ...


class ResultBuilder:
    """Helper that providers use to build a consistent ProviderResult envelope."""

    def __init__(self, provider: str, source_type: str, request: ResearchRequest) -> None:
        self.provider = provider
        self.source_type = source_type
        self.req = request
        self.started = utcnow()
        self._t0 = time.monotonic()
        self.window = capture_window(self.started)
        self.evidence: list[Evidence] = []
        self.observations: list[Observation] = []
        self.limitations: list[str] = []
        self.errors: list[ProviderErrorInfo] = []
        self.retry_count = 0
        self._ev_ids: set[str] = set()

    # -------------------------------------------------------------- evidence
    def add_evidence(
        self,
        *,
        observation_type: ObservationType,
        claim: str,
        source_ref: str,
        confidence: float,
        source_url: str | None = None,
        claim_kind: ClaimKind = ClaimKind.FACT,
        metric: str | None = None,
        value: Any = None,
        observed_at: str | None = None,
        raw: dict[str, Any] | None = None,
        estimate: EstimateRange | None = None,
        content_hash: str | None = None,
        source_type: str | None = None,
    ) -> str:
        eid = evidence_id(
            entity_key=self.req.entity.entity_key,
            provider=self.provider,
            observation_type=observation_type.value,
            source_ref=source_ref,
            capture_window=self.window,
        )
        if eid in self._ev_ids:
            return eid
        now = utcnow()
        raw = _bound_raw(raw or {})
        ev = Evidence(
            evidence_id=eid,
            entity_id=self.req.entity.entity_id,
            provider=self.provider,
            source_type=source_type or self.source_type,
            source_url=source_url,
            observation_type=observation_type,
            observed_at=observed_at,
            captured_at=iso(now),
            expires_at=iso(default_expires_at(now)),
            freshness=freshness_of(now, now),
            claim_kind=claim_kind,
            claim=claim[:MAX_CLAIM],
            metric=metric,
            value=value,
            estimate=estimate,
            raw_reference=raw,
            content_hash=content_hash,
            confidence=max(0.0, min(1.0, confidence)),
        )
        self.evidence.append(ev)
        self._ev_ids.add(eid)
        return eid

    def add_observation(
        self,
        obs_type: ObservationType,
        *,
        metrics: dict[str, float | int | None] | None = None,
        items: list[dict[str, Any]] | None = None,
        evidence_refs: list[str] | None = None,
        claim_kind: ClaimKind = ClaimKind.FACT,
        max_items: int = 300,
    ) -> Observation:
        obs = Observation(
            observation_id=observation_id(
                entity_key=self.req.entity.entity_key,
                provider=self.provider,
                observation_type=obs_type.value,
                capture_window=self.window,
            ),
            entity_id=self.req.entity.entity_id,
            provider=self.provider,
            type=obs_type,
            captured_at=iso(utcnow()),
            claim_kind=claim_kind,
            metrics=metrics or {},
            items=(items or [])[:max_items],
            evidence_refs=evidence_refs or [],
        )
        self.observations.append(obs)
        return obs

    def error(self, code: ErrorCode, message: str) -> None:
        self.errors.append(ProviderErrorInfo(code=code, message=message[:300]))

    def limit(self, text: str) -> None:
        if text not in self.limitations:
            self.limitations.append(text)

    # ----------------------------------------------------------------- build
    def build(self, status: ProviderStatus | None = None) -> ProviderResult:
        if status is None:
            if self.observations and not self.errors:
                status = ProviderStatus.SUCCESS
            elif self.observations:
                status = ProviderStatus.PARTIAL
            elif any(e.code is ErrorCode.RATE_LIMITED for e in self.errors):
                status = ProviderStatus.RATE_LIMITED
            elif self.errors:
                status = ProviderStatus.FAILED
            else:
                status = ProviderStatus.PARTIAL
        done = utcnow()
        return ProviderResult(
            provider=self.provider,
            entity_id=self.req.entity.entity_id,
            status=status,
            observations=self.observations,
            evidence=self.evidence,
            limitations=self.limitations,
            captured_at=iso(done),
            errors=self.errors,
            telemetry=ProviderTelemetry(
                research_id=self.req.research_id,
                provider=self.provider,
                entity_id=self.req.entity.entity_id,
                started_at=iso(self.started),
                completed_at=iso(done),
                duration_ms=int((time.monotonic() - self._t0) * 1000),
                status=status,
                items_observed=sum(len(o.items) or 1 for o in self.observations),
                evidence_created=len(self.evidence),
                error_code=self.errors[0].code if self.errors else None,
                retry_count=self.retry_count,
            ),
        )


def _bound_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep raw references small: excerpts truncated, lists capped (spec #17)."""
    out: dict[str, Any] = {}
    for k, v in list(raw.items())[:20]:
        if isinstance(v, str):
            out[k] = v[:MAX_EXCERPT]
        elif isinstance(v, list):
            out[k] = [x[:200] if isinstance(x, str) else x for x in v[:20]]
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, dict):
            out[k] = {kk: (vv[:200] if isinstance(vv, str) else vv) for kk, vv in list(v.items())[:20]}
        else:
            out[k] = str(v)[:200]
    return out


def status_for_error(code: ErrorCode) -> ProviderStatus:
    if code is ErrorCode.RATE_LIMITED:
        return ProviderStatus.RATE_LIMITED
    if code in (
        ErrorCode.PROVIDER_NOT_CONFIGURED,
        ErrorCode.SOURCE_UNAVAILABLE,
        ErrorCode.INVALID_DOMAIN,
        ErrorCode.SSRF_BLOCKED,
    ):
        return ProviderStatus.UNAVAILABLE
    if code is ErrorCode.UNSUPPORTED_SOURCE:
        return ProviderStatus.UNSUPPORTED
    return ProviderStatus.FAILED


def simple_result(
    provider: str,
    source_type: str,
    request: ResearchRequest,
    status: ProviderStatus,
    code: ErrorCode | None,
    message: str,
    limitations: list[str] | None = None,
) -> ProviderResult:
    b = ResultBuilder(provider, source_type, request)
    if code:
        b.error(code, message)
    for lim in limitations or []:
        b.limit(lim)
    return b.build(status)


async def run_provider(provider: IntelligenceProvider, request: ResearchRequest, timeout_s: float) -> ProviderResult:
    """Run a provider with a hard timeout; never raises; emits one structured telemetry log line (spec §37)."""
    result = await _run_provider(provider, request, timeout_s)
    log.info("provider_finished", extra={"telemetry": result.telemetry.model_dump(mode="json")})
    return result


async def _run_provider(provider: IntelligenceProvider, request: ResearchRequest, timeout_s: float) -> ProviderResult:
    try:
        return await asyncio.wait_for(provider.collect(request), timeout=timeout_s)
    except TimeoutError:
        return simple_result(
            provider.name,
            provider.source_type,
            request,
            ProviderStatus.FAILED,
            ErrorCode.TIMEOUT,
            f"provider exceeded {timeout_s:.0f}s budget",
        )
    except EngineError as e:
        return simple_result(provider.name, provider.source_type, request, status_for_error(e.code), e.code, e.message)
    except Exception as e:  # noqa: BLE001 - provider bugs must not crash research
        log.exception("provider crashed", extra={"provider": provider.name})
        return simple_result(
            provider.name,
            provider.source_type,
            request,
            ProviderStatus.FAILED,
            ErrorCode.PROVIDER_FAILED,
            f"{type(e).__name__}: {str(e)[:200]}",
        )
