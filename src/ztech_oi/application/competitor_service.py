"""Competitor application service (spec §10, §40).

Owns competitor discovery (search + optional LLM, or caller hints only) and the
conversion of discovery observations into canonical `Competitor` entities.
"""

from __future__ import annotations

from ..domain.identity import entity_id, entity_key, normalize_domain
from ..domain.models import CompanyProfile, Competitor, Prospect, ProspectInput, ProviderResult
from ..domain.taxonomy import ObservationType, RelationshipType
from ..integrations.search import NullSearch
from ..providers.base import IntelligenceProvider, ResearchRequest, run_provider
from ..providers.competitors import CompetitorDiscoveryProvider


class CompetitorService:
    def __init__(self, discovery: IntelligenceProvider, *, provider_timeout_s: float = 60.0) -> None:
        self.discovery = discovery
        self.provider_timeout_s = provider_timeout_s

    @property
    def name(self) -> str:
        return self.discovery.name

    def _request(self, rid: str, prospect: Prospect, pinput: ProspectInput) -> ResearchRequest:
        return ResearchRequest(
            research_id=rid,
            entity=prospect,
            prospect=prospect,
            is_prospect=True,
            context={"known_competitors": [h.model_dump() for h in pinput.known_competitors]},
            products_services=pinput.products_services,
        )

    async def discover(self, rid: str, prospect: Prospect, pinput: ProspectInput, *, hints_only: bool = False) -> ProviderResult:
        """Run discovery. `hints_only` uses only caller-provided competitors (no search calls)."""
        provider = CompetitorDiscoveryProvider(NullSearch()) if hints_only else self.discovery
        return await run_provider(provider, self._request(rid, prospect, pinput), self.provider_timeout_s)

    @staticmethod
    def competitors_from(result: ProviderResult, limit: int) -> list[Competitor]:
        obs = next((o for o in result.observations if o.type is ObservationType.COMPETITOR_CANDIDATES), None)
        out: list[Competitor] = []
        seen: set[str] = set()
        for item in obs.items if obs else []:
            key = entity_key(domain=item.get("domain"), company_name=item["company_name"])
            if key in seen:
                continue
            seen.add(key)
            out.append(
                Competitor(
                    entity_id=entity_id(key),
                    entity_key=key,
                    company_name=item["company_name"],
                    domain=normalize_domain(item.get("domain")) or None,
                    location=item.get("location"),
                    relationship_type=RelationshipType(item["relationship_type"]),
                    relationship_confidence=float(item["relationship_confidence"]),
                    reason=item["reason"],
                    evidence_refs=item.get("evidence_refs", []),
                    discovered_via=item["discovered_via"],
                )
            )
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def with_profile(c: Competitor, profile: CompanyProfile) -> Competitor:
        """Attach the observed profile; location comes from the competitor's OWN site (never assumed)."""
        location = c.location or (profile.locations[0] if profile.locations else None)
        return c.model_copy(update={"profile": profile, "location": location})
