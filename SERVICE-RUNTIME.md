# OI Service Runtime (Phase I2)

How the Opportunity Intelligence service is started, where it stores data, and
what ZTech is allowed to assume about it.

The service is an **independent local REST service**. It is not a library inside
ZTech, not a Python process inside Electron, and it never shares ZTech's
database.

## Run it

Development / manual launch (foreground, loopback only):

```
.venv\Scripts\python scripts\run_local_service.py
.venv\Scripts\python scripts\run_local_service.py --port 8099
```

Equivalent manual form, which is what the tests and probes use:

```
.venv\Scripts\python -m uvicorn ztech_oi.adapters.rest:app --host 127.0.0.1 --port 8099
```

The launcher refuses any non-loopback bind address, so the engine cannot be
accidentally exposed on a LAN or public interface by a mistyped flag.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/v1/health` | liveness + schema version |
| GET | `/v1/engine` | engine identity, provider list, configuration summary, confidence producers |
| POST | `/v1/research` | run research, return the canonical `IntelligenceReport` |
| POST | `/v1/competitors/discover` | candidate competitors only |
| GET | `/v1/reports/{research_id}` | canonical report, optionally section-filtered |
| POST | `/v1/reports/{research_id}/analyze` | re-run opportunity analysis on a stored report |
| GET | `/v1/timeline` | research timeline for a `research_id`, entity or domain |

`response=summary` on `/v1/research` returns a counts-and-conclusions summary
instead of the full report.

## Storage

The SQLite store is **owned by this service** and lives at:

```
<repo>/data/ztech_oi.sqlite3
```

`<repo>/data/` is git-ignored, so a runtime database is never committed.

`ZTECH_OI_DB_PATH` may override the location, but the launcher explicitly
refuses a value named `whatsapp.db`: that is ZTech's database and this service
must never open it. No OI tables exist in `whatsapp.db`.

ZTech persists only the minimum association needed to reopen intelligence for a
lead (`lead_id` <-> `research_id` / `snapshot_id`); the full report is re-fetched
from OI by id.

## Credentials

Six provider credentials are read from the **environment only** and are never
required to start:

```
BRAVE_API_KEY  SERPER_API_KEY  LLM_API_KEY
META_ACCESS_TOKEN  SERPAPI_API_KEY  X_BEARER_TOKEN
```

With none of them set the service still boots and still answers every route.
Each provider then reports its own honest status in `provider_status`:

| Provider | No credentials |
| --- | --- |
| website | works (no key needed) |
| content | works (LLM only improves classification) |
| competitor discovery | `unavailable` - needs Brave or Serper |
| meta_ads | `unavailable` |
| google_ads | `unavailable` |
| twitter | `unavailable` |
| linkedin | `unsupported` - always, by design |

An unavailable provider does **not** fail the report. `status` becomes
`partial`, `limitations` explains exactly what was missed, and the collected
evidence is still usable. Only "no provider could observe the prospect at all"
produces `failed`.

Secrets are never serialised: `Settings.describe()` reports only booleans and a
`db_path` basename, and `Settings.__repr__` is overridden for the same reason.

## Lifecycle requirement (production, not yet built)

Phase I2 uses a manual launch. The production requirement, recorded here so it is
not lost, is:

```
ZTech starts
  -> checks OI health (short timeout, failure tolerated)
  -> OI available  OR  honestly unavailable
  -> ZTech continues working either way
```

Concretely:

1. **OI service failure must never crash, hang or degrade Electron.** The health
   check has its own short timeout and a circuit-breaker; after repeated
   failures OI is reported `unavailable` and not probed again until a retry
   window elapses.
2. **Readiness is derived, not stored.** ZTech's own `Ready`/outreach semantics
   are computed from Zuni-SEO evidence and the human approval. OI provider
   availability has **no** effect on whether a lead is ready or may be sent.
   OI is research context, not send capability.
3. **OI never sends.** There is no outreach surface on this service at all.
4. **No secret crosses IPC.** Provider credentials stay in the OI process
   environment. ZTech sends no provider key and receives none back.
5. **Start order is not load-bearing.** If OI is not up yet, ZTech shows
   "opportunity intelligence unavailable" and the user can retry; nothing else
   is blocked.
6. A supervised launcher (auto-start, restart-on-crash, single-instance lock) is
   deliberately deferred to a later batch.

## Test isolation

`pytest` runs fully offline against fakes in `tests/fakes.py`; the provider
suite never opens a socket. When a test run does need a database file, point it
somewhere disposable:

```
$env:ZTECH_OI_DB_PATH = "<repo>/data/ztech_oi_test.sqlite3"
.venv\Scripts\python -m pytest -q
```

The one skipped test is the Node MCP client SDK round-trip, which requires
`npm install` inside `clients/node`.
