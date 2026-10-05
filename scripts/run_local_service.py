"""Local development / manual launcher for the OI REST service (Phase I2).

Development and manual launch only. This is NOT a supervisor, a daemon, a
Windows service or a background watchdog: it binds a loopback socket, prints a
banner, and runs uvicorn in the foreground until interrupted.

Safety properties this script is responsible for:

  * Loopback only. The bind host must resolve to 127.0.0.1 / ::1 / localhost.
    There is no flag to bind a public interface, so a mistake cannot expose the
    research engine to the network.
  * Dedicated database. The SQLite store is placed under <repo>/data, which is
    git-ignored. It can never be pointed at ZTech's whatsapp.db: the path is
    derived from this file's own location, not from an environment variable that
    a caller could point anywhere.
  * No credential requirement. The service starts with every provider
    unavailable when no keys are configured, and reports that honestly per
    provider. Nothing here needs an API key to boot.
  * No provider research is triggered by starting. Research happens only when a
    client calls POST /v1/research.

Usage:
    .venv\\Scripts\\python scripts\\run_local_service.py
    .venv\\Scripts\\python scripts\\run_local_service.py --port 8099
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8099
LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def repo_data_dir() -> Path:
    """The service-owned runtime directory. Never inside ZTech, never shared."""
    d = REPO_ROOT / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_db_path() -> Path:
    """Dedicated, service-owned SQLite file under <repo>/data."""
    override = os.environ.get("ZTECH_OI_DB_PATH")
    if override:
        p = Path(override).expanduser().resolve()
        if p.name == "whatsapp.db":
            raise SystemExit(
                "refusing to run: ZTECH_OI_DB_PATH points at ZTech's whatsapp.db. "
                "Opportunity Intelligence owns its own database."
            )
        return p
    return repo_data_dir() / "ztech_oi.sqlite3"


def assert_loopback(host: str) -> str:
    """Refuse any bind address that is not loopback."""
    h = host.strip().strip("[]")
    if h.lower() in LOOPBACK_NAMES:
        return h
    try:
        addr = ipaddress.ip_address(h)
    except ValueError as e:
        raise SystemExit(f"refusing to bind {host!r}: not an IP address or known loopback name") from e
    if not addr.is_loopback:
        raise SystemExit(
            f"refusing to bind {host!r}: only loopback addresses are allowed. "
            "Exposing this service remotely is out of scope for the local launcher."
        )
    return h


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the OI REST service on loopback (development).")
    ap.add_argument("--host", default=DEFAULT_HOST, help="loopback bind host (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"bind port (default {DEFAULT_PORT})")
    ap.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"])
    args = ap.parse_args(argv)

    host = assert_loopback(args.host)
    if not (1 <= args.port <= 65535):
        raise SystemExit(f"invalid port {args.port}")

    db_path = resolve_db_path()
    os.environ["ZTECH_OI_DB_PATH"] = str(db_path)

    try:
        import uvicorn  # noqa: F401
    except ImportError as e:
        raise SystemExit(
            "uvicorn is not installed. Run: .venv\\Scripts\\python -m pip install -e \".[rest,dev]\""
        ) from e

    # Provider credentials are read from the environment only. Absence is normal and
    # supported: every provider then reports `unavailable` (or `unsupported`) and the
    # report is still usable. We never require a key to boot.
    configured = [
        name
        for name in ("BRAVE_API_KEY", "SERPER_API_KEY", "LLM_API_KEY", "META_ACCESS_TOKEN",
                     "SERPAPI_API_KEY", "X_BEARER_TOKEN")
        if os.environ.get(name)
    ]

    base = f"http://{host}:{args.port}"
    print(f"OI local service\n  bind        {base}\n  db          {db_path}\n"
          f"  health      {base}/v1/health\n"
          f"  providers   {len(configured)}/6 configured"
          f"{'' if configured else ' (all unavailable; service still starts)'}")
    if not configured:
        print("  note        no provider credentials set - expect honest per-provider 'unavailable'.")

    uvicorn.run("ztech_oi.adapters.rest:app", host=host, port=args.port,
                log_level=args.log_level, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
