"""Optional REST adapter (spec #6). `pip install .[rest]`, then:

    uvicorn ztech_oi.adapters.rest:app --port 8080

Thin adapter: validation is done by the domain models / service; no business logic.
"""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from typing import Any

from ..application.container import Engine, build_engine
from ..application.summary import select_sections, summarize
from ..config import Settings
from ..domain.errors import EngineError
from ..domain.taxonomy import ErrorCode

try:
    from fastapi import Body, FastAPI, Query
    from fastapi.responses import JSONResponse
except ImportError as e:  # pragma: no cover
    raise ImportError("REST adapter requires the 'rest' extra: pip install ztech-opportunity-intelligence[rest]") from e

STATUS = {ErrorCode.VALIDATION_ERROR: 400, ErrorCode.INVALID_DOMAIN: 400, ErrorCode.NOT_FOUND: 404, ErrorCode.RATE_LIMITED: 429}

# Local-service protection (ZTech I3/I4).
#
# AUTH_TOKEN_ENV   optional. When set, every route except GET /v1/health requires
#                  `Authorization: Bearer <token>`. Unset = the previous behaviour, so a
#                  manually started development service keeps working unchanged.
# INSTANCE_ID_ENV  optional. Echoed by /v1/health so a supervisor can tell ITS child from
#                  any other process that happens to hold the port.
# Host check       always on. Only loopback Host headers are served, which is what stops a
#                  web page from reaching this service through DNS rebinding.
# Neither value is ever logged, serialised into a report, describe() or an error body.
AUTH_TOKEN_ENV = "ZTECH_OI_AUTH_TOKEN"
INSTANCE_ID_ENV = "ZTECH_OI_INSTANCE_ID"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_UNSET = object()


def _host_name(host_header: str | None) -> str:
    """The host part of a Host header, lower-cased, port and IPv6 brackets removed."""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end != -1 else ""
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _env_or_none(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v else None


def create_app(engine_factory=None, *, auth_token: Any = _UNSET, instance_id: Any = _UNSET) -> FastAPI:
    state: dict[str, Engine] = {}
    token = _env_or_none(AUTH_TOKEN_ENV) if auth_token is _UNSET else (auth_token or None)
    instance = _env_or_none(INSTANCE_ID_ENV) if instance_id is _UNSET else (instance_id or None)
    token_bytes = token.encode("utf-8") if token else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state["e"] = (engine_factory or (lambda: build_engine(Settings.from_env())))()
        yield
        await state["e"].aclose()

    app = FastAPI(title="ZTech Opportunity Intelligence", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def _local_guard(request, call_next):
        if _host_name(request.headers.get("host")) not in LOOPBACK_HOSTS:
            return JSONResponse({"error": "MISDIRECTED_REQUEST", "message": "only loopback hosts are served"},
                                status_code=421)
        if token_bytes is not None and not (request.method == "GET" and request.url.path == "/v1/health"):
            header = request.headers.get("authorization") or ""
            scheme, _, presented = header.partition(" ")
            ok = scheme.lower() == "bearer" and hmac.compare_digest(presented.strip().encode("utf-8"), token_bytes)
            if not ok:
                return JSONResponse({"error": "UNAUTHORIZED", "message": "a valid bearer token is required"},
                                    status_code=401)
        return await call_next(request)

    @app.exception_handler(EngineError)
    async def _engine_error(_req, exc: EngineError):
        return JSONResponse(exc.to_dict(), status_code=STATUS.get(exc.code, 500))

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "schema_version": "1.0", "instance_id": instance}

    @app.get("/v1/engine")
    async def info() -> dict[str, Any]:
        return state["e"].service.engine_info()

    @app.post("/v1/research")
    async def research(body: dict[str, Any] = Body(...), response: str = Query("full", pattern="^(full|summary)$")):
        report = await state["e"].service.analyze_prospect(body.get("prospect", body), body.get("options"))
        return summarize(report) if response == "summary" else report.model_dump(mode="json")

    @app.post("/v1/competitors/discover")
    async def discover(body: dict[str, Any] = Body(...)):
        return await state["e"].service.discover_competitors(body.get("prospect", body), int(body.get("max_competitors", 5)))

    @app.get("/v1/reports/{research_id}")
    async def report(research_id: str, sections: str | None = None):
        try:
            return select_sections(state["e"].service.get_report(research_id), sections.split(",") if sections else None)
        except ValueError as e:
            return JSONResponse({"error": "VALIDATION_ERROR", "message": str(e)}, status_code=400)

    @app.post("/v1/reports/{research_id}/analyze")
    async def analyze(research_id: str):
        return state["e"].service.analyze_opportunity(research_id)

    @app.get("/v1/timeline")
    async def timeline(research_id: str | None = None, domain: str | None = None, company_name: str | None = None):
        return state["e"].service.get_timeline(research_id=research_id, domain=domain, company_name=company_name)

    return app


app = create_app()
