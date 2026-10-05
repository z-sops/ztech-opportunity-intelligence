"""Optional REST adapter (spec #6). `pip install .[rest]`, then:

    uvicorn ztech_oi.adapters.rest:app --port 8080

Thin adapter: validation is done by the domain models / service; no business logic.
"""

from __future__ import annotations

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


def create_app(engine_factory=None) -> FastAPI:
    state: dict[str, Engine] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state["e"] = (engine_factory or (lambda: build_engine(Settings.from_env())))()
        yield
        await state["e"].aclose()

    app = FastAPI(title="ZTech Opportunity Intelligence", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(EngineError)
    async def _engine_error(_req, exc: EngineError):
        return JSONResponse(exc.to_dict(), status_code=STATUS.get(exc.code, 500))

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "schema_version": "1.0"}

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
