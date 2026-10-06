"""Local-service protection for the REST adapter (ZTech I3/I4).

The token is optional and backward compatible: unset = the previous behaviour.
When set, every route except GET /v1/health needs `Authorization: Bearer <token>`.
The Host check is always on and only serves loopback Host headers.
"""

from __future__ import annotations

import logging

import pytest
from conftest import PROSPECT, make_engine

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ztech_oi.adapters import rest  # noqa: E402
from ztech_oi.adapters.rest import create_app  # noqa: E402
from ztech_oi.config import Settings  # noqa: E402

TOKEN = "t0k3n-" + "a" * 58
INSTANCE = "inst-7f3c9e"
LOCAL = "http://127.0.0.1"
NON_HEALTH = [
    ("get", "/v1/engine", None),
    ("post", "/v1/research?response=summary", {"prospect": PROSPECT}),
    ("post", "/v1/competitors/discover", {"prospect": PROSPECT}),
    ("get", "/v1/reports/res_missing", None),
    ("post", "/v1/reports/res_missing/analyze", None),
    ("get", "/v1/timeline?domain=sweetcrumb.com", None),
    ("get", "/openapi.json", None),
]


def _client(market, search_market, **kw):
    return TestClient(create_app(lambda: make_engine(market, search=search_market), **kw), base_url=LOCAL)


def _call(client, method, path, body, headers=None):
    fn = getattr(client, method)
    return fn(path, json=body, headers=headers) if method == "post" else fn(path, headers=headers)


def test_token_unset_keeps_the_previous_behaviour(market, search_market):
    with _client(market, search_market, auth_token=None) as c:
        assert c.get("/v1/engine").status_code == 200
        assert c.post("/v1/research?response=summary", json={"prospect": PROSPECT}).status_code == 200


@pytest.mark.parametrize("method,path,body", NON_HEALTH)
def test_every_non_health_route_requires_the_token(market, search_market, method, path, body):
    with _client(market, search_market, auth_token=TOKEN) as c:
        for headers in (None, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN},
                        {"Authorization": "Basic " + TOKEN}, {"Authorization": "Bearer "}):
            r = _call(c, method, path, body, headers)
            assert r.status_code == 401, (path, headers, r.status_code)
            assert r.json()["error"] == "UNAUTHORIZED"
            assert TOKEN not in r.text


def test_the_right_token_is_accepted(market, search_market):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    with _client(market, search_market, auth_token=TOKEN) as c:
        assert c.get("/v1/engine", headers=auth).status_code == 200
        r = c.post("/v1/research?response=summary", json={"prospect": PROSPECT}, headers=auth)
        assert r.status_code == 200 and r.json()["research_id"]
        assert c.get("/v1/reports/res_missing", headers=auth).status_code == 404
        # scheme is case-insensitive, like HTTP itself
        assert c.get("/v1/engine", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200


def test_health_is_exempt_and_echoes_the_instance_id(market, search_market):
    with _client(market, search_market, auth_token=TOKEN, instance_id=INSTANCE) as c:
        body = c.get("/v1/health").json()
        assert body == {"status": "ok", "schema_version": "1.0", "instance_id": INSTANCE}
    with _client(market, search_market, auth_token=None, instance_id=None) as c:
        assert c.get("/v1/health").json()["instance_id"] is None


def test_values_come_from_the_environment_by_default(monkeypatch, market, search_market):
    monkeypatch.setenv(rest.AUTH_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(rest.INSTANCE_ID_ENV, INSTANCE)
    with _client(market, search_market) as c:
        assert c.get("/v1/health").json()["instance_id"] == INSTANCE
        assert c.get("/v1/engine").status_code == 401
        assert c.get("/v1/engine", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


@pytest.mark.parametrize("base", ["http://127.0.0.1:8099", "http://localhost:8099", "http://[::1]:8099", LOCAL])
def test_loopback_hosts_are_served(market, search_market, base):
    with TestClient(create_app(lambda: make_engine(market, search=search_market), auth_token=None), base_url=base) as c:
        assert c.get("/v1/health").status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8099", "127.0.0.1.evil.example", "10.0.0.5:8099", ""])
def test_non_loopback_host_headers_are_refused(market, search_market, host):
    with _client(market, search_market, auth_token=None) as c:
        for path in ("/v1/health", "/v1/engine"):
            r = c.get(path, headers={"Host": host})
            assert r.status_code == 421, (host, path)
            assert r.json()["error"] == "MISDIRECTED_REQUEST"


def test_host_check_runs_before_auth(market, search_market):
    with _client(market, search_market, auth_token=TOKEN) as c:
        r = c.get("/v1/engine", headers={"Host": "evil.example", "Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 421


def test_token_and_instance_id_are_never_serialised(monkeypatch, market, search_market, caplog):
    monkeypatch.setenv(rest.AUTH_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(rest.INSTANCE_ID_ENV, INSTANCE)
    caplog.set_level(logging.DEBUG)
    s = Settings.from_env()
    assert TOKEN not in repr(s) and TOKEN not in str(s.describe())
    auth = {"Authorization": f"Bearer {TOKEN}"}
    with _client(market, search_market) as c:
        bodies = [
            c.get("/v1/engine", headers=auth).text,
            c.post("/v1/research", json={"prospect": PROSPECT}, headers=auth).text,
            c.get("/v1/reports/res_missing", headers=auth).text,
            c.post("/v1/research", json={"prospect": {"company_name": "x", "domain": "localhost"}}, headers=auth).text,
            c.get("/v1/engine").text,
        ]
    for text in bodies:
        assert TOKEN not in text
        assert INSTANCE not in text
    assert TOKEN not in caplog.text


def test_launcher_reports_auth_state_but_never_the_token(monkeypatch, capsys, tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "run_local_service", Path(__file__).resolve().parents[1] / "scripts" / "run_local_service.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    called = {}
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.update(k))
    monkeypatch.setenv(rest.AUTH_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("ZTECH_OI_DB_PATH", str(tmp_path / "oi.sqlite3"))
    assert launcher.main(["--port", "8123"]) == 0
    out = capsys.readouterr().out
    assert "local auth  required" in out
    assert TOKEN not in out
    assert called["port"] == 8123 and called["host"] == "127.0.0.1"
