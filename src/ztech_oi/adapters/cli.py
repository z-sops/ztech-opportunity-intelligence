"""CLI adapter (internal Python API wrapper). Prints canonical JSON to stdout.

ztech-oi research --company "Allbirds" --domain allbirds.com --industry "footwear" --competitor "Rothy's=rothys.com"
ztech-oi report res_...           ztech-oi timeline --domain allbirds.com
ztech-oi info                      ztech-oi schema > intelligence_report.schema.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from ..application.container import build_engine
from ..application.summary import summarize
from ..config import Settings
from ..domain.errors import EngineError
from ..domain.models import IntelligenceReport
from ..observability.logging import configure_logging


def _hint(s: str) -> dict[str, Any]:
    name, _, dom = s.partition("=")
    return {"company_name": name.strip(), "domain": dom.strip() or None}


async def _run(args: argparse.Namespace) -> Any:
    if args.cmd == "schema":
        return IntelligenceReport.model_json_schema()
    engine = build_engine(Settings.from_env())
    try:
        svc = engine.service
        if args.cmd == "research":
            payload = {
                "company_name": args.company,
                "domain": args.domain,
                "location": args.location,
                "industry": args.industry,
                "products_services": args.offer or [],
                "known_competitors": [_hint(c) for c in args.competitor or []],
            }
            opts = {"max_competitors": args.max_competitors, "discover_competitors": not args.no_discovery}
            report = await svc.analyze_prospect(payload, opts)
            return summarize(report) if args.summary else report.model_dump(mode="json")
        if args.cmd == "report":
            return svc.get_report(args.research_id).model_dump(mode="json")
        if args.cmd == "analyze":
            return svc.analyze_opportunity(args.research_id)
        if args.cmd == "timeline":
            return svc.get_timeline(research_id=args.research_id, domain=args.domain, company_name=args.company)
        if args.cmd == "info":
            return svc.engine_info()
    finally:
        await engine.aclose()
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ztech-oi")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("research", help="run full opportunity research")
    r.add_argument("--company", required=True)
    r.add_argument("--domain")
    r.add_argument("--location")
    r.add_argument("--industry")
    r.add_argument("--offer", action="append", help="product/service (repeatable)")
    r.add_argument("--competitor", action="append", help="'Name=domain.com' (repeatable)")
    r.add_argument("--max-competitors", type=int, default=3)
    r.add_argument("--no-discovery", action="store_true", help="only use --competitor hints")
    r.add_argument("--summary", action="store_true")
    for name in ("report", "analyze"):
        p = sub.add_parser(name)
        p.add_argument("research_id")
    t = sub.add_parser("timeline")
    t.add_argument("--research-id")
    t.add_argument("--domain")
    t.add_argument("--company")
    sub.add_parser("info")
    sub.add_parser("schema")
    args = ap.parse_args(argv)
    configure_logging(Settings.from_env().log_level)
    try:
        out = asyncio.run(_run(args))
    except EngineError as e:
        print(json.dumps(e.to_dict(), indent=2), file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
