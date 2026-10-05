# ZTech Competitive & Opportunity Intelligence Engine

A standalone Python subsystem for **ZTech Lead Generation**. You give it a prospect company. It collects defensible market intelligence about the prospect and its competitors, turns every observation into evidence with provenance, compares the prospect against those competitors, and returns a versioned, machine-readable **IntelligenceReport**. That report covers opportunities, an explainable score, evidence-backed sales angles and a timeline.

It does **not** send outreach, run campaigns, or touch ZTech's database. ZTech consumes it later through **MCP**.

```
Prospect ─┬─ website · content · Meta ads · Google ads · X · LinkedIn(unsupported)
          └─ competitor discovery ─► same providers for each competitor
                     ▼
     Evidence (+provenance, freshness, conflicts) ─► Comparisons ─► Snapshot diff
                     ▼
      Signals ─► Opportunities ─► Score ─► Sales angles ─► Timeline ─► IntelligenceReport (schema 1.0)
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[rest,dev]"
cp .env.example .env           # fill in the keys you have (all optional)
set -a && source .env && set +a

pytest -q                      # 175 tests, fully offline (Node contract test runs if clients/node is installed)
ztech-oi research --company "Allbirds" --domain allbirds.com --industry "footwear" \
    --competitor "Rothy's=rothys.com" --summary
ztech-oi-mcp                   # MCP server on stdio (for ZTech / Claude Desktop / any MCP client)
uvicorn ztech_oi.adapters.rest:app --port 8080   # optional REST adapter
```

## What works without any API key

| Provider | Needs | Source | Status without key |
|---|---|---|---|
| `website` | nothing | robots.txt, sitemap.xml, homepage + up to 8 key pages (profile, contact, tech, CTAs, offers, business model) | **REAL** |
| `content` | nothing | RSS/Atom feed → article page dates → sitemap lastmod; topic-relevant 60-day counts | **REAL** |
| `competitor_discovery` | `BRAVE_API_KEY` or `SERPER_API_KEY` (or `known_competitors`) | web search (+ optional LLM resolution) | uses `known_competitors` only |
| `meta_ads` | `META_ACCESS_TOKEN` | **official** Meta Ad Library API | `unavailable` (NOT CONFIGURED) |
| `google_ads` | `SERPAPI_API_KEY` | Google Ads Transparency Center via SerpApi (labelled third-party) | `unavailable` (NOT CONFIGURED) |
| `twitter` | `X_BEARER_TOKEN` | **official** X API v2; handle taken from the company's own website | `unavailable` (NOT CONFIGURED) |
| `linkedin` | — | none legitimate | `unsupported` (always) |

The minimum useful setup is no keys plus `known_competitors`: you get website and content intelligence for the prospect and every named competitor, with real comparisons. Adding a search key turns on automatic competitor discovery.

## Phase report (spec §45)

The full per-phase report is in [PHASE-REPORT.md](PHASE-REPORT.md). Summary:

| Item | Status |
|---|---|
| Core engine working | ✅ end-to-end, deterministic |
| Canonical report validated | ✅ Pydantic + exported JSON Schema (`schemas/`), schema test guards drift |
| Persistence working | ✅ SQLite (WAL) behind a repository interface; in-memory implementation for tests |
| Repeated-run behaviour tested | ✅ evidence de-duplication, snapshot diff, score history, restart survival |
| At least one real research path | ✅ website + content providers need no keys (see *Verification* below) |
| Competitor analysis | ✅ 12 comparison dimensions incl. topic-scoped ("7 wedding-cake articles vs your 1") |
| Evidence provenance | ✅ every number → `evidence_refs` → source URL/claim/confidence |
| Signals / opportunities / timeline / angles | ✅ closed taxonomies, inference-labelled |
| MCP server + tools tested | ✅ 8 tools, in-process, **real stdio subprocess**, and **official JS SDK (Node) contract test** |
| Failure cases tested | ✅ timeout, rate limit, SSRF, malformed, huge, partial, not-found… |
| Full suite green | ✅ 175 passed · ruff clean · mypy clean · 89 % line coverage |

**Verification note.** The build sandbox has no outbound internet, so providers were verified against realistic offline fixtures served through the real HTTP stack (`httpx.MockTransport` plus a fake DNS resolver): sitemaps, RSS feeds, JSON-LD, Meta, SerpApi and X API payloads. The first run against live websites is the next step. See `TESTING.md` §Live smoke test.

## Repository layout

```
src/ztech_oi/
  domain/        models (Pydantic), taxonomy (closed enums), identity (stable ids, freshness), errors
  security/      url_guard — SSRF / URL sanitisation with DNS checks
  net/           http (bounded, SSRF-safe client), parsing (HTML / robots / sitemap / feeds)
  integrations/  search (Brave, Serper), llm (any OpenAI-compatible endpoint)
  providers/     website, content, competitors, ads (Meta, Google), social (X, LinkedIn)
  engine/        index, comparison, diff, signals, opportunities, scoring, timeline, angles, conflicts
  persistence/   repository (interface), sqlite, memory
  application/   research_service (the ONLY entry point) → competitor_service, opportunity_service; container, summary
  adapters/      mcp_server, rest, cli   — thin, no business logic
schemas/   exported JSON Schemas (v1)      examples/  sample request / outputs
tests/     175 tests + offline fakes       scripts/   export_schema.py
clients/node/  Node.js MCP client contract test (ZTech integration proof, spec §43)
```

Docs: [PHASE-REPORT](PHASE-REPORT.md) · [ARCHITECTURE](ARCHITECTURE.md) · [API-CONTRACT](API-CONTRACT.md) · [MCP-CONTRACT](MCP-CONTRACT.md) · [DATA-SCHEMA](DATA-SCHEMA.md) · [LIMITATIONS](LIMITATIONS.md) · [TESTING](TESTING.md)
