"""Read-only index over one research run's observations, evidence and provider statuses.

Engines query structured metrics through this index — never free-text claims.
"""

from __future__ import annotations

from statistics import mean

from ..domain.models import Evidence, Observation
from ..domain.taxonomy import FreshnessState, ObservationType, ProviderStatus

OBS_PROVIDER = {
    ObservationType.WEBSITE_HOMEPAGE: "website",
    ObservationType.WEBSITE_PAGE_INVENTORY: "website",
    ObservationType.WEBSITE_COMPANY_PROFILE: "website",
    ObservationType.CONTENT_INVENTORY: "content",
    ObservationType.CONTENT_TOPICS: "content",
    ObservationType.ADS_META: "meta_ads",
    ObservationType.ADS_GOOGLE: "google_ads",
    ObservationType.SOCIAL_TWITTER: "twitter",
    ObservationType.SOCIAL_LINKEDIN: "linkedin",
    ObservationType.COMPETITOR_CANDIDATES: "competitor_discovery",
}


class RunIndex:
    def __init__(
        self,
        observations: list[Observation],
        evidence: list[Evidence],
        provider_status: dict[str, dict[str, ProviderStatus]],
    ) -> None:
        self.observations = observations
        self.evidence = {e.evidence_id: e for e in evidence}
        self.provider_status = provider_status
        self._obs: dict[tuple[str, ObservationType], Observation] = {}
        for o in observations:
            self._obs[(o.entity_id, o.type)] = o

    def obs(self, entity_id: str, obs_type: ObservationType) -> Observation | None:
        o = self._obs.get((entity_id, obs_type))
        if o is None:
            return None
        # Expired observations are not current: treat as unobserved (spec #33).
        refs = [self.evidence[r] for r in o.evidence_refs if r in self.evidence]
        if refs and all(e.freshness is FreshnessState.EXPIRED for e in refs):
            return None
        return o

    def metric(self, entity_id: str, obs_type: ObservationType, key: str) -> float | None:
        o = self.obs(entity_id, obs_type)
        if o is None:
            return None
        v = o.metrics.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    def status(self, entity_id: str, provider: str) -> ProviderStatus | None:
        return self.provider_status.get(entity_id, {}).get(provider)

    def available(self, entity_id: str, provider: str) -> bool:
        return self.status(entity_id, provider) in (ProviderStatus.SUCCESS, ProviderStatus.PARTIAL)

    def metric_confidence(self, entity_id: str, obs_type: ObservationType, metric: str | None = None) -> float:
        o = self.obs(entity_id, obs_type)
        if o is None:
            return 0.0
        refs = [self.evidence[r] for r in o.evidence_refs if r in self.evidence]
        if metric:
            m = [e for e in refs if e.metric == metric]
            if m:
                return max(e.confidence for e in m)
        return mean(e.confidence for e in refs) if refs else 0.5

    def refs_for(self, entity_id: str, obs_type: ObservationType, metric: str | None = None, limit: int = 4) -> list[str]:
        o = self.obs(entity_id, obs_type)
        if o is None:
            return []
        if metric:
            m = [r for r in o.evidence_refs if r in self.evidence and self.evidence[r].metric == metric]
            if m:
                return m[:limit]
        return o.evidence_refs[:limit]

    def profile(self, entity_id: str) -> dict:
        o = self.obs(entity_id, ObservationType.WEBSITE_COMPANY_PROFILE)
        return o.items[0] if o and o.items else {}
