# Architecture

## 1. Layering (spec §6 — mandatory separation)

```
 adapters/   mcp_server.py   rest.py   cli.py          ← transport only: validate → call → serialise
                   │            │        │
 application/      └──── ResearchService ────┘          ← the ONLY entry point; orchestration
                      ├─ CompetitorService            ← discovery + Competitor entities (+location)
                      └─ OpportunityService           ← pure analysis: compare → diff → signals → opps → score → angles
                           │
 engine/     comparison · diff · signals · opportunities · scoring · timeline · angles · conflicts
                           │   (pure, deterministic, no I/O)
 providers/  website · content · competitors · ads(meta, google) · social(x, linkedin)
                           │   (all I/O, behind IntelligenceProvider)
 net/ + security/   SafeHttpClient ← UrlGuard (SSRF)     integrations/ search · llm
 persistence/       Repository (interface) ← SQLiteRepository | InMemoryRepository
 domain/            models · taxonomy · identity · errors
```

Dependencies point downward only. The engine never imports providers, adapters or persistence. Adapters never contain business rules: the MCP tool handlers are about ten lines each.

## 2. Research pipeline (`ResearchService._pipeline`)

1. **Validate** `ProspectInput` / `ResearchOptions`. Domains are normalised; private, invalid and IP-literal hosts are rejected with `INVALID_DOMAIN`.
2. **Idempotency.** If `options.idempotency_key` is already known, answer from the stored job without any network call: the stored report, `IN_PROGRESS` while it runs, its stored failure, or `IDEMPOTENCY_CONFLICT` for another prospect. A new job claims its key atomically. At startup, `RUNNING` jobs older than 15 minutes are closed as `FAILED` (`interrupted`); reports are never touched.
3. **Prospect collection and competitor discovery** run concurrently.
   - Per-entity order: `website` → `content` (needs the sitemap and feeds from `website`) → `meta_ads`, `google_ads`, `twitter`, `linkedin` in parallel.
   - Providers share facts through `ResearchRequest.context` (sitemap entries, feeds, social handles). The X handle is never guessed.
4. **Competitor collection.** The same provider chain runs for the top *N* competitors, with bounded concurrency (`entity_concurrency=3`).
5. **Evidence.** Evidence is merged by stable `evidence_id`, then conflict detection runs (`engine/conflicts.py`).
6. **Snapshot.** An immutable snapshot is built from the observations and per-entity provider statuses. The previous snapshot is loaded.
7. **Analysis** (`_analyse`, pure and re-runnable offline): comparisons → diff → signals (entity, change and gap) → opportunities → score → sales angles.
8. **Timeline** contains only engine-owned events. Dated facts (article published, ad started) appear at their real dates.
9. **Persist.** Entities and evidence are upserted (idempotent); snapshots are appended; signals, opportunities and the report are stored.

## 3. Key design rules

| Rule | Where enforced |
|---|---|
| FACT / ESTIMATE / INFERENCE never collapsed | `ClaimKind` on every Evidence/Signal/Opportunity; `Opportunity.claim_kind` is `Literal[INFERENCE]`; `EstimateRange` only valid with `claim_kind=estimate` |
| Engines read **structured metrics**, never regex over claim text | `engine/index.py` (`RunIndex.metric`) |
| Absence of evidence ≠ evidence of absence | `engine/comparison.py`: unavailable → null; Meta zero outside EU/UK → null; homepage-link inventory zeros → null |
| 1 vs 2 is not a gap | `interpret()` requires ratio ≥ 1.5 **and** an absolute minimum delta per dimension |
| Provider failure ≠ removal | `engine/diff.py`: availability transitions emit `PROVIDER_AVAILABILITY_CHANGED` only |
| Single snapshot cannot prove change | "NEW_*", "INCREASE", "PRODUCT_LAUNCH", "EXPANSION" signals come only from diffs |
| Expired evidence is not current | `RunIndex.obs()` ignores observations whose evidence is all `expired` |
| AI never creates facts | LLM output is stored as INFERENCE, confidence capped at 0.75 / 0.85, and must cite observed inputs (grounding checks drop invented names/themes) |
| Zero opportunities is valid | No filler opportunity or angle is ever generated |

## 4. Stable identity and idempotency (spec §32)

- `entity_key` = `dom:<normalised domain>` or `name:<normalised name>|<location>`; `entity_id = ent_<sha256(key)[:16]>`. The same company gets the same id in every run.
- `evidence_id` = hash(entity_key, provider, observation_type, source_ref, **capture window = UTC day**). Re-running on the same day upserts instead of duplicating. A new day creates new evidence, so history is preserved.
- `research_id`, `snapshot_id` are sortable random ids (run-scoped).
- `comparison_id`, `signal_id`, `opportunity_id`, `event_id` are hashes of research_id plus content, so they are deterministic within a run.

## 5. Concurrency and isolation

There is no global mutable state. Each research builds its own evidence set, and providers receive a per-entity `context` dict. `SafeHttpClient` has a global concurrency semaphore and a per-host minimum interval. Concurrent researches for different prospects cannot mix data (tested).

## 6. Security boundary (spec §36)

- `UrlGuard` checks scheme, port, credentials, IP literals and internal suffixes, then resolves DNS and requires **every** address to be globally routable. This runs on **every redirect hop**.
- Responses are streamed and cut at `max_bytes`. XML is parsed with `defusedxml`: entity declarations, internal DTD subsets and external references are forbidden; a plain DOCTYPE (for example the public sitemap DTD) is allowed and never fetched. Gzip sitemaps are bounded after decompression.
- Crawling is bounded: ≤5 sitemap files, ≤2000 URLs, ≤8 page fetches per entity, ≤6 article fetches, ≤50 ads/posts.
- robots.txt is respected for page fetches.
- Secrets live only in `Settings` private fields. They never appear in reports, MCP responses or logs: tokens are stripped from stored URLs, the logger redacts them, and httpx/httpcore URL logging is forced to WARNING.
- The MCP surface is 8 research tools. There is no SQL, filesystem, shell or credential tool, and every input is validated by Pydantic.

## 7. Repository structure vs spec §40 (justification)

| Spec suggestion | This repo | Why |
|---|---|---|
| `domain/entities, evidence, signals, opportunities, timeline` sub-packages | `domain/models.py`, `taxonomy.py`, `identity.py`, `errors.py` | One model module keeps the canonical schema in one place; it is exported to a single JSON Schema and drift-tested |
| `application/research_service.py, competitor_service.py, opportunity_service.py` | same three files | as specified |
| `providers/website, google_ads, meta_ads, linkedin, twitter` folders | `providers/website.py, content.py, competitors.py, ads.py (Meta+Google), social.py (X+LinkedIn)` | ad and social providers share helpers (entity matching, token stripping, themes); folders add no isolation |
| `scoring/` | `engine/scoring.py` | all deterministic analysis lives in `engine/` (no I/O) |
| `mcp/` | `adapters/mcp_server.py` (+ `rest.py`, `cli.py`) | every transport is an adapter of the same service |
| `schemas/` | `schemas/*.json` generated from code | single source of truth |
| — | `security/`, `net/`, `integrations/`, `observability/` | SSRF guard, bounded HTTP, vendor-neutral search/LLM, redacting logs are cross-cutting |
| — | `clients/node/` | proves spec §43 with the official JS MCP SDK |

## 8. Extending

- **New provider.** Implement `collect(ResearchRequest) -> ProviderResult` using `ResultBuilder`, add an `ObservationType` if needed, and register it in `application/container.py`. Engines only change if the provider adds a new comparison dimension (`engine/comparison.py:DIMENSIONS`).
- **Zuni-SEO.** Add a `ZuniSeoProvider` that calls the Zuni-SEO MCP and emits `website.*` observations. This is deliberately **not** wired yet (spec §44).
- **Postgres.** Implement `Repository`; nothing above persistence changes.
- **Remote MCP.** `ztech-oi-mcp --transport streamable-http --port 8765`; the engine is unchanged.
