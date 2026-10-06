# API Contract (internal Python API, REST, CLI)

## 1. Internal Python API — `ResearchService`

```python
from ztech_oi.application.container import build_engine
engine = build_engine()                  # Settings.from_env()
svc = engine.service
report = await svc.analyze_prospect({"company_name": "Acme", "domain": "acme.com"}, {"max_competitors": 3})
await engine.aclose()
```

| Method | Returns | Network |
|---|---|---|
| `analyze_prospect(prospect, options=None)` | `IntelligenceReport` | yes |
| `research_company(prospect)` | `IntelligenceReport` (no competitors) | yes |
| `discover_competitors(prospect, max_competitors=5)` | `dict` | search only |
| `research_competitors(prospect, competitors)` | `IntelligenceReport` | yes |
| `analyze_opportunity(research_id)` | `dict` (re-derived analysis) | **no** |
| `get_report(research_id)` | `IntelligenceReport` (freshness recomputed) | no |
| `get_timeline(research_id=… \| domain=… \| company_name=…)` | `dict` | no |
| `engine_info()` | `dict` | no |

Inputs can be dicts or the Pydantic models. All outputs are JSON-serialisable (`report.model_dump(mode="json")`).

### `ProspectInput`
| field | type | rules |
|---|---|---|
| `company_name` | str | required, 1-120, not blank |
| `domain` | str? | normalised (`https://www.X.com/p` → `x.com`); invalid/private → `INVALID_DOMAIN` |
| `location`, `industry` | str? | ≤120 |
| `products_services` | str[] | ≤10; drives search queries and "relevant articles" counts |
| `known_competitors` | `{company_name, domain?}[]` | ≤10; become `user_provided` competitors; duplicates and the prospect itself are dropped |
| `icp` | `{industries[], locations[], keywords[]}`? | only feeds the `icp_fit` score component |

Unknown fields are rejected (`extra="forbid"`).

### `ResearchOptions`
`max_competitors` 0-5 (default 3) · `discover_competitors` bool (false = only `known_competitors`) · `providers` allow-list of provider names · `idempotency_key` 1-128 chars of `A-Z a-z 0-9 . _ : -`, one key per caller intent. A repeated key never runs providers again: a finished run returns its stored report, a running one answers `409 IN_PROGRESS` (retryable), a failed one returns its stored failure with `details.idempotent_replay: true` (terminal), and a key reused for a different prospect answers `409 IDEMPOTENCY_CONFLICT`.

## 2. Error model

Every failure raises `EngineError(code, message, retryable, details)`. Its serialised form is `{"error": code, "message": ..., "retryable": ..., "details"?: ...}`.

| Code | Meaning | REST |
|---|---|---|
| `VALIDATION_ERROR` | bad input | 400 |
| `INVALID_DOMAIN` | domain invalid / private / unresolvable | 400 |
| `NOT_FOUND` | unknown research_id / prospect | 404 |
| `RATE_LIMITED` | upstream throttled (retryable) | 429 |
| `INTERNAL_ERROR` | unexpected | 500 |

Provider-level problems **never** raise. They show up in the report as provider statuses, `errors[]` and `limitations[]` with these codes: `SOURCE_UNAVAILABLE`, `PROVIDER_NOT_CONFIGURED`, `UNSUPPORTED_SOURCE`, `RATE_LIMITED`, `TIMEOUT`, `SSRF_BLOCKED`, `RESPONSE_TOO_LARGE`, `MALFORMED_RESPONSE`, `COMPANY_NOT_FOUND`, `INSUFFICIENT_EVIDENCE`, `PROVIDER_FAILED`, `PARTIAL_RESEARCH`.

Report-level `status`:
- `completed`: every configured provider succeeded.
- `partial`: some provider failed, was rate-limited, was partial, or was unavailable for a reason other than not-configured.
- `failed`: nothing could be observed about the prospect. `COMPANY_NOT_FOUND` is in limitations and the report is still returned.

## 3. REST adapter (optional, `pip install .[rest]`)

`uvicorn ztech_oi.adapters.rest:app --port 8080`

| Method & path | Body / query | Response |
|---|---|---|
| `GET /v1/health` | — | `{"status":"ok","schema_version":"1.0"}` |
| `GET /v1/engine` | — | engine info |
| `POST /v1/research?response=full\|summary` | `{"prospect": ProspectInput, "options"?: ResearchOptions}` | report / summary |
| `POST /v1/competitors/discover` | `{"prospect":…, "max_competitors"?: n}` | discovery result |
| `GET /v1/reports/{research_id}?sections=a,b` | — | report / sections |
| `POST /v1/reports/{research_id}/analyze` | — | re-derived analysis |
| `GET /v1/timeline?research_id=…\|domain=…\|company_name=…` | — | merged timeline |

## 4. CLI

```
ztech-oi research --company NAME [--domain D] [--location L] [--industry I] [--offer X]... \
                  [--competitor "Name=domain"]... [--max-competitors N] [--no-discovery] [--summary]
ztech-oi report RESEARCH_ID     ztech-oi analyze RESEARCH_ID
ztech-oi timeline --domain D    ztech-oi info     ztech-oi schema
```
Output is JSON on stdout. Exit code 2 means `EngineError` (JSON on stderr).
