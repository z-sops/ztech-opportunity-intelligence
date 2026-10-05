"""In-memory repository with the same contract as SQLiteRepository (tests, ephemeral use)."""

from __future__ import annotations

import threading

from ..domain.models import (
    Competitor,
    Entity,
    Evidence,
    IntelligenceReport,
    Opportunity,
    ResearchJob,
    Signal,
    Snapshot,
)


class InMemoryRepository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.jobs: dict[str, ResearchJob] = {}
        self.entities: dict[str, Entity] = {}
        self.competitors: dict[tuple[str, str, str], Competitor] = {}
        self.evidence: dict[str, Evidence] = {}
        self.snapshots: dict[str, Snapshot] = {}
        self.signals: dict[str, Signal] = {}
        self.opportunities: dict[str, Opportunity] = {}
        self.reports: dict[str, IntelligenceReport] = {}

    def save_job(self, job: ResearchJob) -> None:
        with self._lock:
            self.jobs[job.research_id] = job.model_copy(deep=True)

    def get_job(self, research_id: str) -> ResearchJob | None:
        return self.jobs.get(research_id)

    def find_job_by_idempotency_key(self, key: str) -> ResearchJob | None:
        return next((j for j in self.jobs.values() if j.idempotency_key == key), None)

    def upsert_entity(self, entity: Entity) -> None:
        with self._lock:
            self.entities[entity.entity_id] = Entity.model_validate(entity.model_dump(include=set(Entity.model_fields)))

    def get_entity(self, entity_id: str) -> Entity | None:
        return self.entities.get(entity_id)

    def save_competitors(self, prospect_entity_id: str, research_id: str, competitors: list[Competitor]) -> None:
        with self._lock:
            for c in competitors:
                self.competitors[(prospect_entity_id, c.entity_id, research_id)] = c

    def upsert_evidence(self, evidence: list[Evidence]) -> int:
        new = 0
        with self._lock:
            for e in evidence:
                new += 0 if e.evidence_id in self.evidence else 1
                self.evidence[e.evidence_id] = e
        return new

    def get_evidence(self, evidence_ids: list[str]) -> list[Evidence]:
        return [self.evidence[i] for i in evidence_ids if i in self.evidence]

    def count_evidence(self) -> int:
        return len(self.evidence)

    def save_snapshot(self, snapshot: Snapshot) -> None:
        with self._lock:
            if snapshot.snapshot_id in self.snapshots:
                raise ValueError("snapshots are immutable")
            self.snapshots[snapshot.snapshot_id] = snapshot.model_copy(deep=True)

    def get_snapshot(self, snapshot_id: str) -> Snapshot | None:
        return self.snapshots.get(snapshot_id)

    def latest_snapshot_before(self, prospect_entity_key: str, captured_at: str) -> Snapshot | None:
        cands = [
            s for s in self.snapshots.values() if s.prospect_entity_key == prospect_entity_key and s.captured_at < captured_at
        ]
        return max(cands, key=lambda s: s.captured_at, default=None)

    def list_snapshots(self, prospect_entity_key: str) -> list[Snapshot]:
        return sorted(
            (s for s in self.snapshots.values() if s.prospect_entity_key == prospect_entity_key), key=lambda s: s.captured_at
        )

    def save_signals(self, research_id: str, signals: list[Signal]) -> None:
        with self._lock:
            for s in signals:
                self.signals[s.signal_id] = s

    def save_opportunities(self, research_id: str, opportunities: list[Opportunity]) -> None:
        with self._lock:
            for o in opportunities:
                self.opportunities[o.opportunity_id] = o

    def save_report(self, report: IntelligenceReport) -> None:
        with self._lock:
            self.reports[report.research_id] = report.model_copy(deep=True)

    def get_report(self, research_id: str) -> IntelligenceReport | None:
        return self.reports.get(research_id)

    def latest_report(self, prospect_entity_key: str, before: str | None = None) -> IntelligenceReport | None:
        cands = [
            r
            for r in self.reports.values()
            if r.prospect.entity_key == prospect_entity_key and (before is None or r.generated_at < before)
        ]
        return max(cands, key=lambda r: r.generated_at, default=None)

    def list_reports(self, prospect_entity_key: str) -> list[IntelligenceReport]:
        return sorted(
            (r for r in self.reports.values() if r.prospect.entity_key == prospect_entity_key), key=lambda r: r.generated_at
        )

    def close(self) -> None:
        return None
