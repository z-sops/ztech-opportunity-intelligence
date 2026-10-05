"""Local launcher safety: loopback-only bind, service-owned database, no key needed.

These are the guarantees the launcher itself is responsible for. They are cheap
and offline: nothing here starts uvicorn or opens a socket.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("run_local_service", ROOT / "scripts" / "run_local_service.py")
assert SPEC and SPEC.loader
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


# --- bind address ------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "[::1]"])
def test_loopback_hosts_are_accepted(host):
    assert launcher.assert_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.5", "example.com", "8.8.8.8"])
def test_non_loopback_binds_are_refused(host):
    """A mistyped or copied flag must never expose the engine to a network."""
    with pytest.raises(SystemExit):
        launcher.assert_loopback(host)


# --- database ownership ------------------------------------------------------

def test_db_defaults_to_service_owned_data_dir(monkeypatch):
    monkeypatch.delenv("ZTECH_OI_DB_PATH", raising=False)
    path = launcher.resolve_db_path()
    assert path.parent == ROOT / "data"
    assert path.name == "ztech_oi.sqlite3"
    # never ZTech's database, and never inside ZTech
    assert "whatsapp" not in path.name
    assert path.parent != Path(r"C:\Users\EC\Downloads\ZTech")


def test_data_dir_is_git_ignored():
    """A runtime database must never be committable."""
    ignored = subprocess_git_ignored()
    assert ignored is True


def subprocess_git_ignored() -> bool:
    import subprocess

    r = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "-q", "data/ztech_oi.sqlite3"],
        capture_output=True,
    )
    return r.returncode == 0


def test_ztech_database_is_explicitly_refused(monkeypatch):
    monkeypatch.setenv("ZTECH_OI_DB_PATH", r"C:\Users\EC\Downloads\ZTech\whatsapp.db")
    with pytest.raises(SystemExit) as e:
        launcher.resolve_db_path()
    assert "whatsapp.db" in str(e.value)


def test_explicit_override_elsewhere_is_honoured(monkeypatch, tmp_path):
    target = tmp_path / "oi.sqlite3"
    monkeypatch.setenv("ZTECH_OI_DB_PATH", str(target))
    assert launcher.resolve_db_path() == target.resolve()


# --- credentials are optional ------------------------------------------------

def test_service_starts_with_no_credentials(monkeypatch, tmp_path):
    """No API key anywhere -> the launcher must still be able to boot."""
    for name in ("BRAVE_API_KEY", "SERPER_API_KEY", "LLM_API_KEY",
                 "META_ACCESS_TOKEN", "SERPAPI_API_KEY", "X_BEARER_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ZTECH_OI_DB_PATH", str(tmp_path / "oi.sqlite3"))

    booted = {}

    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs):
            booted["app"] = app
            booted.update(kwargs)

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)

    assert launcher.main(["--port", "8123"]) == 0
    assert booted["app"] == "ztech_oi.adapters.rest:app"
    assert booted["host"] == "127.0.0.1"
    assert booted["port"] == 8123
    # loopback bind is enforced even when uvicorn would happily accept anything
    assert booted["host"] in {"127.0.0.1", "::1", "localhost"}


def test_main_refuses_public_bind_before_importing_uvicorn(monkeypatch):
    def explode():
        raise AssertionError("uvicorn must not be imported for a refused bind")

    monkeypatch.setitem(sys.modules, "uvicorn", type("U", (), {"run": staticmethod(explode)}))
    with pytest.raises(SystemExit):
        launcher.main(["--host", "0.0.0.0"])


def test_invalid_port_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("ZTECH_OI_DB_PATH", str(tmp_path / "oi.sqlite3"))
    with pytest.raises(SystemExit):
        launcher.main(["--port", "99999"])


def test_launcher_does_not_hardcode_any_credential(monkeypatch):
    """The launcher must read credentials from the environment, never define them."""
    src = (ROOT / "scripts" / "run_local_service.py").read_text(encoding="utf-8")
    for name in ("BRAVE_API_KEY", "SERPER_API_KEY", "META_ACCESS_TOKEN",
                 "SERPAPI_API_KEY", "X_BEARER_TOKEN", "LLM_API_KEY"):
        assert name in src
        assert f'{name}="' not in src and f"{name}='" not in src
