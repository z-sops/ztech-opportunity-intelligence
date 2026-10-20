# Project update log — ZTech Opportunity Intelligence

Running log for the Opportunity Intelligence engine as it becomes part of the
ZTech cloud SaaS. Newest first.

**Owner:** ZuniTech engineering
**Last updated:** 2026-10-20

> This repository is one of the **three** ZTech repositories. `z-sops/zuni-seo`
> is a separate ZuniTech product and an external integration dependency; it is
> inspected read-only and is never modified from here.
>
> The full cross-repository audit lives in the ZTech Lead Generation
> repository: `docs/AUDIT-BASELINE-2026-10-20.md`. This log records only the
> work done in **this** repository.

---

## 2026-10-20 — F0 foundation (branch `feat/cloud-saas-f0-foundation`)

**Base:** `main` @ `03e8de8` (verified against `origin/main`, 0 ahead / 0 behind)

### What was done

| Item | Result |
|---|---|
| CI | `.github/workflows/ci.yml` — Ubuntu + Windows, Python 3.11 + 3.13: install → pytest → ruff → mypy → exported-schema drift check. Plus a **Node MCP contract job** (the ZTech ↔ OI integration proof) and a **container image job** that builds the image and asserts `/v1/health` answers. |
| Container image | `Dockerfile` + `.dockerignore` — python:3.12-slim, non-root uid 10001, a `/data` volume for the SQLite store, a `HEALTHCHECK`, and the service bound to `0.0.0.0:8099`. |
| Lint | `ruff` was configured but never run. It reported **4 findings**; 3 were auto-fixed and 1 (a false positive on the `AUTH_TOKEN_ENV` *name*) got an explicit `# noqa` with a reason. `ruff check` now passes. |
| Types | `mypy` was configured but never run. It reported **1 error** (`ErrorCode(err.get("error"))` could receive `None`); fixed with an explicit `str()`. `mypy src` now passes on all 48 source files. |
| REST contract | The `research_id` path parameter was **unvalidated** on the REST routes while the MCP adapter already enforced `^[a-z]+_[A-Za-z0-9_]+$`. A malformed id reached the database layer instead of being rejected at the edge. Both report routes now use the same pattern and return **422** for a malformed id, and a well-formed but unknown id returns **404 NOT_FOUND** — never a 500. Covered by `tests/test_rest_report_routes.py` (5 new tests). |

### Verified locally

```
python -m venv .venv
pip install -e ".[rest,dev]" ruff mypy

python -m pytest -q          -> 252 passed, 1 skipped  (was 247 passed, 1 skipped)
python -m ruff check src tests scripts        -> All checks passed!   (was 4 errors)
python -m mypy src            -> Success: no issues found in 48 source files   (was 1 error)
```

Container command path verified outside a container (Docker's daemon was not
running in this environment, so the image build itself is verified by the CI
`image` job, not locally):

```
pip install ".[rest]"
python -m uvicorn ztech_oi.adapters.rest:app --host 0.0.0.0 --port 8099 --no-access-log
  GET /v1/health  (Host: 127.0.0.1)  -> 200 {"status":"ok","schema_version":"1.0","instance_id":null}
  GET /v1/engine  (Host: evil.example) -> 421 MISDIRECTED_REQUEST
```

The second line matters: the Host-header check is the DNS-rebinding defence,
and it is still on in the container.

**No live provider was contacted.** The test suite is fully offline —
`tests/fakes.py` provides a fake internet through `httpx.MockTransport` and a
fake DNS resolver.

### The most important finding, unchanged

`POST /v1/research` is **synchronous** and takes roughly 20-90 s for a prospect
plus three competitors (`LIMITATIONS.md:35`). ZTech's gateway
(`OpportunityIntelligenceGateway.js`) allows it **30 seconds** and has no
polling path. In normal use the opportunity research feature times out
client-side while the server keeps working.

This is audit gap **G-44** and it is rated **Critical**. It is not fixed here
because the fix is a contract change on both sides — an async job API on this
service plus a polling client in ZTech — and that belongs to F3, where it can
be changed and tested as one unit.

### Still open in this repository

| Gap | Risk | Planned |
|---|---|---|
| G-44 — synchronous research vs. a 30 s client timeout | Critical | F3 |
| G-46 — no tenant scoping, no CORS, one static bearer token, no rate limiting | High | F2/F3 |
| G-47 — no migration tooling: `SCHEMA_VERSION = 1` with `CREATE TABLE IF NOT EXISTS` on every open and **no upgrade path** | Medium | F2 |
| G-48 — ~~no CI, no container image~~ | — | **done in F0** |
| G-49 — ~~unvalidated `research_id` on the REST routes~~ | — | **done in F0** |
| SQLite is single-writer; many concurrent writers need a Postgres `Repository` (`LIMITATIONS.md:36`) | Medium | F2 |

### Review state (2026-10-20)

The review branch is **pushed** and the review PR is **open as a draft**:
https://github.com/z-sops/ztech-opportunity-intelligence/pull/1
(the head of the review branch -> base `main`).
Nothing is merged, deployed or released.

**CI IS GREEN:** https://github.com/z-sops/ztech-opportunity-intelligence/actions/runs/39399534884
— 6/6 jobs: ubuntu + windows on Python 3.11 + 3.13, the **Node MCP contract
test**, and the **container image build**.

The first push was refused with *"refusing to allow a Personal Access Token to
create or update workflow `.github/workflows/ci.yml` without `workflow`
scope"* — the available token had only `repo`. A `git push --dry-run` does
**not** detect this; it passed, and only the real push was refused.

**The CI found two real defects in this service**, both invisible to the
Windows-only local environment:

1. The ZTech-database guard in the launcher compared `Path().name`, which is
   the whole string for a Windows path read on Linux, so the guard never fired
   there. It is now on the last path component, split on **both** separators, so
   it refuses ZTech's database on every platform.
2. A completed run that observed nothing could not be replayed. A report whose
   status is `failed` is a **completed** run — the research finished and
   honestly reported that nothing could be observed — but the idempotency path
   treated it as a terminal failure and raised, so a repeated key lost the
   report **and** the research_id. A run that produced a report now always
   replays that report; only a run that produced **no** report raises its
   stored failure.

   The Node MCP contract test found this. It is the first time that test had
   ever run — it had always been skipped, because the Node client was never
   installed.

Local verification: `pytest` 269 passed / 0 skipped, `ruff` clean, `mypy` clean
on 48 files, the exported JSON schema unchanged, and the Node MCP contract test
passing end to end.

### The one skipped test

`pytest -q -rs` reports:

```
SKIPPED [1] tests/test_spec_gaps.py:364: node or @modelcontextprotocol/sdk
            not installed (cd clients/node && npm install)
269 passed, 1 skipped
```

It is the **Node MCP contract test** - the proof that ZTech (a Node/Electron
main process) can talk to this engine over MCP stdio. It skips when
`clients/node/node_modules` is not present, which is the case in a plain
Python-only checkout. The CI `contract` job installs the client and runs it,
so the contract is covered in CI - and that job is what found the idempotency
defect described above. It is a genuine skip, not a hidden failure: the test
reports itself through pytest's skip mechanism and the count is printed above.

### The pending cloud work

The job lifecycle (G-44) is **durable inside the service**: a run survives
the caller, an interrupted run is closed at startup by
`recover_interrupted_jobs()`, and the idempotency key makes a retry safe. What
is **not** implemented yet is the browser-independent, cloud-side job
recovery: there is no per-workspace scoping (G-46), no queue, no resumable
result store and no authenticated caller identity, so the job routes inherit
the service's single shared bearer token. Those are F2/F4 work and are
tracked in the audit.

### Note on a dependency lock

The resolved versions are recorded in the CI output, but a committed
`requirements-lock.txt` was **not** added in F0: the runtime closure includes
`pywin32` (pulled in on Windows), so a single cross-platform lock file would
break the Linux container build. A platform-neutral lock, or a per-platform
lock set, is the right answer and is scheduled with the deployment work.

### F2 — what this service owes the cloud product (2026-10-20)

**Branch:** `feat/f2-cloud-saas-architecture` (based on the F0/F1 head
`2ecd68b`). **Status: DESIGN FOR REVIEW — no implementation has started.**

The ZTech cloud architecture is in the ZTech Lead Generation repository,
`docs/F2-CLOUD-SAAS-ARCHITECTURE.md`. This service's place in it:

| Concern | What F2 needs from this repository |
|---|---|
| **Tenant scoping** | Every report, entity, evidence row and job must belong to a workspace. The `Repository` protocol (`src/ztech_oi/persistence/repository.py`) gains a `workspace_id` on every entity, and the REST layer resolves it from the authenticated caller. |
| **Durable jobs** | `POST /v1/research` is already asynchronous (`/v1/research/jobs`). It needs to move to the platform's `job_queue` with a lease, so a run survives the service and the caller independently. |
| **Idempotency** | The existing per-caller-intent `idempotency_key` becomes the platform's `provider_invocations` key, so a retry cannot run a second research. |
| **Per-workspace credentials** | The single shared bearer token is replaced by a per-workspace credential, so a job can only spend the workspace's own credits. |
| **Reuse** | The **engine** is reused unchanged: the providers, the scoring, the comparisons, the signals, the opportunities, the sales angles, the deterministic analysis. Only the edges change. |

**The recommended isolation model for this service is one instance per tenant
first** (the strongest available boundary, at the cost of scale), moving to a
shared instance with tenant-scoped storage once the isolation tests exist. See
question Q7 in the F2 plan.

**This repository is not modified in the F0/F1 state.** The changes above land
in their own milestones, each with its own acceptance criteria, so the
existing 269 tests and the green CI remain the baseline.

### Next

F0 for the Legal repository, then F1.
