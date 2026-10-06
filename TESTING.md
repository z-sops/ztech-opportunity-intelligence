# Testing & Tester Handoff (Orbit)

## 1. Run everything

```bash
pip install -e ".[rest,dev]" && pip install ruff mypy pytest-cov
pytest -q                                   # 175 passed (offline, ~10 s)
pytest -q --cov=ztech_oi --cov-report=term  # 89 % line coverage
(cd clients/node && npm install && PYTHON=python3 npm test)   # official JS MCP SDK contract test (also run by pytest when installed)
ruff check src tests scripts && mypy src    # both clean
python scripts/export_schema.py             # regenerate schemas/ + examples/ (then re-run pytest)
```

No network is needed. `tests/fakes.py` provides a fake internet (`httpx.MockTransport`), fake DNS, fake search and a fake LLM. Fixture sites are generated relative to "now" so the 30/60/90-day windows are stable.

## 2. Suite map

| File | Type | Covers |
|---|---|---|
| `test_security_and_http.py` | unit | URL sanitising, private/CGNAT/IPv6/metadata IPs, DNS→private, redirect→private, redirect loops, huge bodies, declared content-length, 429 retry/recover, timeouts, malformed JSON, log redaction, gzip bombs |
| `test_parsing_and_identity.py` | unit | domain/name normalisation, stable ids, evidence capture window, freshness, URL classification, robots, sitemaps and feeds (malformed, plain DOCTYPE allowed, entities/internal subsets/external refs refused), RSS/Atom, HTML extraction |
| `test_providers.py` | provider contract | every provider × success / not-configured / unavailable / rate-limited / malformed / huge; envelope invariants; robots respected; LLM grounding; token stripping; timeout & crash wrapping |
| `test_engine.py` | unit | comparison null-rules, min-delta, reliable vs unreliable zeros, expired evidence, conflicts, diff rules (no-history, identical, new/removed, provider-failure ≠ removal), scoring redistribution & determinism |
| `test_idempotency_i5.py` | integration | replay of finished / running / failed keys with zero provider calls, cross-prospect conflict, atomic claim, concurrent same key, unsafe keys, interrupted-job recovery, REST 409 mapping |
| `test_service_integration.py` | integration | happy path, repeated research + change detection + evidence dedup, idempotency key, known competitors, partial research, company not found, invalid inputs, allow-list, Meta end-to-end, offline re-analysis, concurrency isolation |
| `test_persistence.py` | persistence | **restart persistence** on SQLite, cross-restart diff, repository contract (SQLite + memory), immutable snapshots, isolated schema |
| `test_mcp.py` | MCP | tool surface (exact set, no forbidden tools), full flow over MCP, input validation, **real stdio subprocess** |
| `test_rest_and_schema.py` | schema / REST | published JSON Schema = code, sample output validates, REST endpoints & error codes |
| `test_spec_gaps.py` | unit + integration + contract | topic-scoped comparisons & topical opportunities, Meta/Google/X depth, business model, competitor location, PARTIAL_RESEARCH, provider telemetry logs, CTA/services/topic-tagged page diffs, Google & X diffs, change-opportunity signal refs, service split, no-fake-zero regression, **Node MCP client contract** |

## 3. Spec §38 scenario checklist

| Scenario | Test |
|---|---|
| missing company | `test_invalid_inputs_are_rejected[payload0/1]` |
| invalid domain | `…[payload2/3]`, MCP `INVALID_DOMAIN` |
| duplicate competitors | `test_discovery_hints_dedup_and_self_exclusion`, `test_known_competitors_without_search` |
| same company mistaken for competitor | same two tests + `test_discovery_search_excludes_directories_and_prospect` |
| provider unavailable | `test_meta_not_configured`, `test_partial_research_when_competitor_site_down` |
| provider timeout | `test_run_provider_timeout_and_crash_never_raise`, `test_timeout_maps_to_timeout_code` |
| rate limit | `test_rate_limit_retries_then_raises`, `test_meta_api_errors[17]`, `test_discovery_search_rate_limited` |
| empty ad results | `test_meta_zero_outside_eu_is_marked_unreliable` |
| stale evidence | `test_expired_evidence_is_excluded_from_comparisons`, `test_freshness_states` |
| conflicting evidence | `test_conflicting_evidence_is_kept_and_downweighted` |
| duplicate evidence | `test_repeated_research_detects_changes_and_dedups_evidence` |
| partial research | `test_partial_research_when_competitor_site_down` |
| bad URLs / private URLs | `test_sanitize_rejects_bad_urls`, `test_dns_rebinding_to_private_is_blocked`, `test_redirect_to_private_network_is_blocked`, `test_website_private_dns_is_blocked` |
| malformed provider response | `test_meta_malformed_response`, `test_website_malformed_sitemap_falls_back_honestly`, `test_malformed_json_raises_malformed_response` |
| huge provider response | `test_huge_response_is_cut_off`, `test_website_huge_page_is_reported_not_crashed` |
| repeated research | `test_repeated_research_detects_changes_and_dedups_evidence`, `test_idempotency_key_returns_same_report` |
| restart persistence | `test_restart_persistence_and_cross_restart_diff` |
| unreachable site must not yield "0 articles" | `test_unresolvable_domain_never_yields_fake_zero_content` |

## 4. Acceptance criteria for the tester

1. `pytest -q` is green, and `ruff`/`mypy` are clean.
2. `ztech-oi-mcp` lists exactly the 8 tools in MCP-CONTRACT.md, from both the Python client and `clients/node` (JS SDK).
3. With **no keys**, a real research on a public site returns `website`/`content` = success, `meta_ads`/`google_ads`/`twitter` = `unavailable` with `PROVIDER_NOT_CONFIGURED`, and `linkedin` = `unsupported`. No ad or social numbers appear anywhere.
4. Every `opportunities[].evidence_refs` id exists in `evidence[]`, and every opportunity has `claim_kind = inference`.
5. Re-running the same prospect on the same day does not increase the `evidence` row count. The second report has `previous_snapshot_id` set.
6. No API key value appears in any report, MCP response or log line.
7. Invalid/private domains are rejected with `INVALID_DOMAIN`, and no HTTP request is made.

## 5. Live smoke test (run on a networked machine)

```bash
export ZTECH_OI_DB_PATH=./live.sqlite3
ztech-oi research --company "Allbirds" --domain allbirds.com --industry footwear \
   --competitor "Rothy's=rothys.com" --competitor "Veja=veja-store.com" --no-discovery --summary > live1.json
jq '.provider_status, .key_comparisons, .opportunities' live1.json
# with keys: export BRAVE_API_KEY=...; META_ACCESS_TOKEN=...; META_AD_COUNTRIES=DE,FR
ztech-oi research --company "Allbirds" --domain allbirds.com --industry footwear --summary > live2.json
ztech-oi timeline --domain allbirds.com | jq '.score_history'
```

Check for: sitemap-based inventory (`inventory_from_sitemap: 1`), dated articles from a feed, plausible competitors, and no errors other than the documented not-configured ones. Report any site where the inventory or dates look wrong, because those become new parsing fixtures.
