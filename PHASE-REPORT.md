# Phase Report (spec §45)

For each phase: what was implemented, the files involved, the architecture decision, the tests, known limitations, real vs placeholder, security implications, and remaining work.

> **Process note (honest).** Spec §41/§47 asks for an architecture review **before** coding, then phase-by-phase delivery. On request this build was delivered in one pass and then hardened in a second "spec-gap" pass. The architecture that would have been reviewed is in `ARCHITECTURE.md`. The phases below are documented retroactively against the final code. Totals: **175 tests green · ruff clean · mypy clean (two mypy versions) · 89 % line coverage**.

| Phase | Implemented | Key files | Architecture decision | Tests | Real vs placeholder | Security | Known limitations / remaining |
|---|---|---|---|---|---|---|---|
| **OI-1 Foundation** | Pydantic domain models, closed taxonomies, error model, stable ids, freshness, research-job model, repository interface | `domain/*`, `persistence/repository.py` | `extra="forbid"` models are the boundary contract; ids are content hashes (§32) | `test_parsing_and_identity.py`, `test_rest_and_schema.py` (schema drift) | n/a | input validation everywhere; invalid/private domains → `INVALID_DOMAIN` | — |
| **OI-2 Website** | robots.txt, sitemap(s) inventory, homepage + ≤8 key pages, profile (contact, services/products, prices, offers, CTAs, tech, locations, **business model**), page kinds | `providers/website.py`, `net/parsing.py`, `net/http.py`, `security/url_guard.py` | engines read structured metrics; zero from a homepage-link inventory is never treated as zero | `test_providers.py` (website_*), `test_spec_gaps.py` (business model) | **REAL**, no key needed | SSRF per redirect hop, size/time caps, robots respected, defusedxml | JS-rendered sites only partly observed |
| **OI-2b Content** | feed → page-date → sitemap-lastmod chain; 30/60/90-day counts; **topic-relevant 60-day count**; title topics; optional LLM themes (inference) | `providers/content.py` | content runs only after the website provider confirms the site is reachable (never fake "0 articles") | `test_providers.py` (content_*), `test_spec_gaps.py::test_unresolvable_domain_never_yields_fake_zero_content` | **REAL** | bounded article fetches | undated articles → null windows |
| **OI-3 Competitor discovery** | caller hints, Brave/Serper search, optional grounded LLM resolution, official-domain resolution, directory & self exclusion, **competitor location from its own site** | `providers/competitors.py`, `application/competitor_service.py` | relationship confidence documented; ungrounded LLM names dropped | `test_providers.py` (discovery_*), `test_spec_gaps.py` (location, hints-only no search) | REAL with search key; hints-only without | search keys never logged | not exhaustive (≤4 queries) |
| **OI-4 Comparison** | 12 deterministic dimensions incl. **topic-scoped** `relevant_content_60d`, `relevant_ad_creatives` | `engine/comparison.py`, `engine/index.py` | ratio ≥ 1.5 **and** minimum delta; untrusted zeros → null | `test_engine.py`, `test_spec_gaps.py` | n/a | — | topic = prospect's `products_services` (needs input) |
| **OI-5 Advertising** | Meta: official Ad Library API, entity match, offers, CTAs, landing domains, topic relevance, **video vs non-video via media_type filter**; Google: SerpApi transparency, formats, **defensive ad-copy extraction**, offers, topics, landing | `providers/ads.py` | EU/UK coverage rule; spend never reported | `test_providers.py`, `test_spec_gaps.py` | REAL with keys; Google labelled third-party | tokens stripped from stored URLs | SerpApi copy field names not verified live; Meta creative type only video/non-video |
| **OI-6 Social** | X API v2: posts, **replies = customer conversations**, rule-based labels (announcement/product_launch/offer/event/hiring), themes, engagement, topic relevance; LinkedIn UNSUPPORTED | `providers/social.py` | handle only from the company's own site; labels are INFERENCE | `test_providers.py`, `test_spec_gaps.py` | X REAL with paid token; LinkedIn unsupported by design | bearer token never logged | latest ≤50 posts only |
| **OI-7 Signals** | 21-type closed taxonomy from observations, changes and gaps (incl. MESSAGING_CHANGE, SOCIAL_ACTIVITY_INCREASE, X-based PRODUCT_LAUNCH) | `engine/signals.py` | change-type signals only from diffs | `test_service_integration.py`, `test_spec_gaps.py` | n/a | — | — |
| **OI-7b Snapshots / diff** | website pages (+**topic-tagged new URLs**, page-inventory summary), **CTA & services changes**, offers/prices, content, Meta, **Google**, **X**; provider-failure ≠ removal | `engine/diff.py` | every Change carries `channel` | `test_engine.py`, `test_spec_gaps.py` | n/a | — | — |
| **OI-8 Opportunities** | 8 types; topical titles ("… around wedding cakes"); change-driven opportunities cite **both change_refs and signal_refs** | `engine/opportunities.py`, `application/opportunity_service.py` | always INFERENCE; zero is valid | `test_engine.py`, `test_spec_gaps.py` | n/a | — | — |
| **OI-9 Score** | 9 explainable components, weight redistribution, score history | `engine/scoring.py` | deterministic; not-configured providers excluded from research confidence | `test_engine.py` | n/a | — | weights are initial judgement, tune with real outcomes |
| **OI-10 Timeline** | engine-owned events, dated facts at real dates, multi-run merge | `engine/timeline.py` | no ZTech lifecycle events | `test_service_integration.py` | n/a | — | — |
| **OI-11 Sales angles** | template-based, evidence-cited, `do_not_claim` guard-rails | `engine/angles.py` | no AI-written claims | `test_service_integration.py` | n/a | — | — |
| **OI-12 MCP** | 8 tools, stdio + streamable-http; summary/full/sections | `adapters/mcp_server.py` | thin adapter over `ResearchService` | `test_mcp.py` (in-process + **real stdio subprocess**), **`clients/node` official JS SDK contract test** | n/a | no SQL/fs/shell/credential tools; Pydantic input validation | — |
| **OI-13 Hardening** | timeouts, retries, rate limits, concurrency caps, conflicts, freshness, restart persistence, **PARTIAL_RESEARCH code**, **per-provider structured telemetry logs** | `net/http.py`, `providers/base.py`, `persistence/sqlite.py`, `observability/logging.py` | logs on stderr as JSON, secrets redacted, httpx URL logging silenced | `test_security_and_http.py`, `test_persistence.py`, `test_spec_gaps.py` | n/a | see ARCHITECTURE §6 | DNS-rebinding race documented |
| **OI-14 Tester handoff** | README, ARCHITECTURE, API/MCP contracts, DATA-SCHEMA, LIMITATIONS, TESTING, this report, JSON Schemas, sample request/outputs | repo root, `schemas/`, `examples/` | schema exported from code, drift-tested | `test_rest_and_schema.py` | n/a | — | **live-internet smoke test still to run** (TESTING.md §5) |

## Definition-of-done checklist (spec §45)

| Requirement | Status |
|---|---|
| Core engine working | ✅ |
| Canonical report validated | ✅ JSON Schema + drift test |
| Persistence working | ✅ SQLite, restart-tested |
| Repeated-run behaviour tested | ✅ |
| At least one real research path working | ✅ in code with no keys (website + content); ⚠️ **not yet executed against the live internet** (sandbox had none) |
| Competitor analysis working | ✅ |
| Evidence provenance working | ✅ |
| Signals working | ✅ |
| Opportunity analysis working | ✅ |
| Timeline working | ✅ |
| MCP server working | ✅ Python and Node clients |
| MCP tools tested | ✅ |
| Failure cases tested | ✅ all §38 scenarios |
| Documentation complete | ✅ |
| Tester package complete | ✅ |
| Full suite green | ✅ 175 passed |
