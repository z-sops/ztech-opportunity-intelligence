"""SQLite repository. Documents are stored as validated JSON with indexed keys.

Isolated, engine-owned schema — this is NOT ZTech's database and never will be.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS research_jobs (
  research_id TEXT PRIMARY KEY,
  prospect_entity_key TEXT NOT NULL,
  status TEXT NOT NULL,
  idempotency_key TEXT UNIQUE,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
  entity_id TEXT PRIMARY KEY,
  entity_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  company_name TEXT NOT NULL,
  domain TEXT,
  data TEXT NOT NULL,
  first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS competitors (
  prospect_entity_id TEXT NOT NULL,
  competitor_entity_id TEXT NOT NULL,
  research_id TEXT NOT NULL,
  relationship_type TEXT NOT NULL,
  relationship_confidence REAL NOT NULL,
  data TEXT NOT NULL,
  PRIMARY KEY (prospect_entity_id, competitor_entity_id, research_id)
);
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  observation_type TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_evidence_entity ON evidence(entity_id);
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY,
  research_id TEXT NOT NULL,
  prospect_entity_key TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snapshots_prospect ON snapshots(prospect_entity_key, captured_at);
CREATE TABLE IF NOT EXISTS signals (
  signal_id TEXT PRIMARY KEY,
  research_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  type TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunities (
  opportunity_id TEXT PRIMARY KEY,
  research_id TEXT NOT NULL,
  type TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
  research_id TEXT PRIMARY KEY,
  prospect_entity_key TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  score INTEGER NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reports_prospect ON reports(prospect_entity_key, generated_at);
"""


class SQLiteRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        with self._lock:
            # executescript manages its own transaction (it COMMITs first), so it runs outside _tx()
            self._conn.executescript(_DDL)
        with self._tx() as c:
            row = c.execute("SELECT version FROM schema_meta").fetchone()
            if row is None:
                c.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row[0] > SCHEMA_VERSION:
                raise RuntimeError(f"database schema v{row[0]} is newer than this engine (v{SCHEMA_VERSION})")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # ------------------------------------------------------------------ jobs
    def save_job(self, job: ResearchJob) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO research_jobs(research_id,prospect_entity_key,status,idempotency_key,created_at,completed_at,data)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(research_id) DO UPDATE SET status=excluded.status,"
                " completed_at=excluded.completed_at, data=excluded.data",
                (
                    job.research_id,
                    job.prospect_entity_key,
                    job.status.value,
                    job.idempotency_key,
                    job.created_at,
                    job.completed_at,
                    job.model_dump_json(),
                ),
            )

    def get_job(self, research_id: str) -> ResearchJob | None:
        rows = self._q("SELECT data FROM research_jobs WHERE research_id=?", (research_id,))
        return ResearchJob.model_validate_json(rows[0][0]) if rows else None

    def find_job_by_idempotency_key(self, key: str) -> ResearchJob | None:
        rows = self._q("SELECT data FROM research_jobs WHERE idempotency_key=?", (key,))
        return ResearchJob.model_validate_json(rows[0][0]) if rows else None

    # -------------------------------------------------------------- entities
    def upsert_entity(self, entity: Entity) -> None:
        base = Entity.model_validate(entity.model_dump(include=set(Entity.model_fields)))
        with self._tx() as c:
            c.execute(
                "INSERT INTO entities(entity_id,entity_key,kind,company_name,domain,data) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(entity_id) DO UPDATE SET company_name=excluded.company_name, domain=excluded.domain,"
                " data=excluded.data, last_seen_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')",
                (base.entity_id, base.entity_key, base.kind.value, base.company_name, base.domain, base.model_dump_json()),
            )

    def get_entity(self, entity_id: str) -> Entity | None:
        rows = self._q("SELECT data FROM entities WHERE entity_id=?", (entity_id,))
        return Entity.model_validate_json(rows[0][0]) if rows else None

    def save_competitors(self, prospect_entity_id: str, research_id: str, competitors: list[Competitor]) -> None:
        with self._tx() as c:
            for comp in competitors:
                c.execute(
                    "INSERT OR REPLACE INTO competitors VALUES (?,?,?,?,?,?)",
                    (
                        prospect_entity_id,
                        comp.entity_id,
                        research_id,
                        comp.relationship_type.value,
                        comp.relationship_confidence,
                        comp.model_dump_json(),
                    ),
                )

    # -------------------------------------------------------------- evidence
    def upsert_evidence(self, evidence: list[Evidence]) -> int:
        new = 0
        with self._tx() as c:
            for ev in evidence:
                cur = c.execute(
                    "INSERT INTO evidence(evidence_id,entity_id,provider,observation_type,captured_at,data)"
                    " VALUES (?,?,?,?,?,?) ON CONFLICT(evidence_id) DO UPDATE SET data=excluded.data,"
                    " captured_at=excluded.captured_at",
                    (ev.evidence_id, ev.entity_id, ev.provider, ev.observation_type.value, ev.captured_at, ev.model_dump_json()),
                )
                new += 1 if cur.rowcount == 1 else 0
        return new

    def get_evidence(self, evidence_ids: list[str]) -> list[Evidence]:
        if not evidence_ids:
            return []
        out: list[Evidence] = []
        for i in range(0, len(evidence_ids), 500):
            chunk = evidence_ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            sql = f"SELECT data FROM evidence WHERE evidence_id IN ({marks})"  # noqa: S608 - only "?" placeholders
            out += [Evidence.model_validate_json(r[0]) for r in self._q(sql, tuple(chunk))]
        return out

    def count_evidence(self) -> int:
        return int(self._q("SELECT COUNT(*) FROM evidence")[0][0])

    # ------------------------------------------------------------- snapshots
    def save_snapshot(self, snapshot: Snapshot) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?)",
                (
                    snapshot.snapshot_id,
                    snapshot.research_id,
                    snapshot.prospect_entity_key,
                    snapshot.captured_at,
                    snapshot.model_dump_json(),
                ),
            )

    def get_snapshot(self, snapshot_id: str) -> Snapshot | None:
        rows = self._q("SELECT data FROM snapshots WHERE snapshot_id=?", (snapshot_id,))
        return Snapshot.model_validate_json(rows[0][0]) if rows else None

    def latest_snapshot_before(self, prospect_entity_key: str, captured_at: str) -> Snapshot | None:
        rows = self._q(
            "SELECT data FROM snapshots WHERE prospect_entity_key=? AND captured_at<? ORDER BY captured_at DESC LIMIT 1",
            (prospect_entity_key, captured_at),
        )
        return Snapshot.model_validate_json(rows[0][0]) if rows else None

    def list_snapshots(self, prospect_entity_key: str) -> list[Snapshot]:
        rows = self._q("SELECT data FROM snapshots WHERE prospect_entity_key=? ORDER BY captured_at", (prospect_entity_key,))
        return [Snapshot.model_validate_json(r[0]) for r in rows]

    # --------------------------------------------------------------- derived
    def save_signals(self, research_id: str, signals: list[Signal]) -> None:
        with self._tx() as c:
            for s in signals:
                c.execute(
                    "INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?)",
                    (s.signal_id, research_id, s.subject_id, s.type.value, s.model_dump_json()),
                )

    def save_opportunities(self, research_id: str, opportunities: list[Opportunity]) -> None:
        with self._tx() as c:
            for o in opportunities:
                c.execute(
                    "INSERT OR REPLACE INTO opportunities VALUES (?,?,?,?)",
                    (o.opportunity_id, research_id, o.type.value, o.model_dump_json()),
                )

    # --------------------------------------------------------------- reports
    def save_report(self, report: IntelligenceReport) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO reports VALUES (?,?,?,?,?)",
                (
                    report.research_id,
                    report.prospect.entity_key,
                    report.generated_at,
                    report.opportunity_score.score,
                    report.model_dump_json(),
                ),
            )

    def get_report(self, research_id: str) -> IntelligenceReport | None:
        rows = self._q("SELECT data FROM reports WHERE research_id=?", (research_id,))
        return IntelligenceReport.model_validate_json(rows[0][0]) if rows else None

    def latest_report(self, prospect_entity_key: str, before: str | None = None) -> IntelligenceReport | None:
        if before:
            rows = self._q(
                "SELECT data FROM reports WHERE prospect_entity_key=? AND generated_at<? ORDER BY generated_at DESC LIMIT 1",
                (prospect_entity_key, before),
            )
        else:
            rows = self._q(
                "SELECT data FROM reports WHERE prospect_entity_key=? ORDER BY generated_at DESC LIMIT 1",
                (prospect_entity_key,),
            )
        return IntelligenceReport.model_validate_json(rows[0][0]) if rows else None

    def list_reports(self, prospect_entity_key: str) -> list[IntelligenceReport]:
        rows = self._q("SELECT data FROM reports WHERE prospect_entity_key=? ORDER BY generated_at", (prospect_entity_key,))
        return [IntelligenceReport.model_validate_json(r[0]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # diagnostic helper used by tests
    def table_counts(self) -> dict[str, int]:
        tables = ["research_jobs", "entities", "competitors", "evidence", "snapshots", "signals", "opportunities", "reports"]
        return {t: int(self._q(f"SELECT COUNT(*) FROM {t}")[0][0]) for t in tables}  # noqa: S608 - fixed list


def dumps(obj: object) -> str:
    return json.dumps(obj, default=str)
