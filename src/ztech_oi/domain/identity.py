"""Stable identities, normalisation and freshness (spec #32, #33).

IDs are content-derived hashes so that repeated research produces the same
identifiers for the same thing instead of uncontrolled duplicates:

  entity_id   = hash(entity_key)                     -- stable forever
  evidence_id = hash(entity_key, provider, observation_type, source_ref, capture_window)
                -- stable within one capture window (UTC day by default)

Run-scoped artefacts (research_id, snapshot_id) are random but sortable.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from .taxonomy import FreshnessState

_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_WS = re.compile(r"\s+")
_LEGAL_SUFFIX = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|co|company|gmbh|plc|pvt|private|s\.a|sa|bv|ag)\.?$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    try:
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        dt = datetime.fromisoformat(v)
    except ValueError:
        # try date-only and a few common formats
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                dt = datetime.strptime(value.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _h(*parts: str, length: int = 16) -> str:
    raw = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:length]


def normalize_name(name: str | None) -> str:
    s = _WS.sub(" ", (name or "").strip().lower())
    s = re.sub(r"[^\w\s&-]", "", s)
    s = _LEGAL_SUFFIX.sub("", s).strip()
    return s


def normalize_domain(value: str | None) -> str:
    """Return a bare lowercase registrable-looking host, or '' if invalid.

    Accepts 'https://www.Example.com/path', 'example.com', 'www.example.com'.
    """
    if not value:
        return ""
    s = value.strip().lower()
    if "://" not in s:
        s = "http://" + s
    try:
        host = urlsplit(s).hostname or ""
    except ValueError:
        return ""
    host = host.rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    if not _DOMAIN_RE.match(host):
        return ""
    return host


def normalize_url(url: str) -> str:
    """Canonical URL for set comparison across snapshots."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((parts.scheme.lower() or "https", host, path, parts.query, ""))


def entity_key(*, domain: str | None, company_name: str, location: str | None = None) -> str:
    d = normalize_domain(domain)
    if d:
        return f"dom:{d}"
    return f"name:{normalize_name(company_name)}|{normalize_name(location)}"


def entity_id(key: str) -> str:
    return f"ent_{_h(key)}"


def evidence_id(*, entity_key: str, provider: str, observation_type: str, source_ref: str, capture_window: str) -> str:
    return f"ev_{_h(entity_key, provider, observation_type, source_ref, capture_window)}"


def observation_id(*, entity_key: str, provider: str, observation_type: str, capture_window: str) -> str:
    return f"obs_{_h(entity_key, provider, observation_type, capture_window)}"


def stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{_h(*parts)}"


def run_id(prefix: str) -> str:
    """Sortable random id for run-scoped artefacts (research, snapshot)."""
    ts = utcnow().strftime("%Y%m%d%H%M%S")
    return f"{prefix}_{ts}_{secrets.token_hex(4)}"


def capture_window(dt: datetime | None = None) -> str:
    """Idempotency window: one UTC day."""
    return (dt or utcnow()).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- freshness
DEFAULT_TTL = timedelta(days=7)
STALE_AFTER = timedelta(days=7)
EXPIRED_AFTER = timedelta(days=30)


def default_expires_at(captured_at: datetime, ttl: timedelta = DEFAULT_TTL) -> datetime:
    return captured_at + ttl


def freshness_of(captured_at: str | datetime | None, now: datetime | None = None) -> FreshnessState:
    if captured_at is None:
        return FreshnessState.UNKNOWN
    dt = parse_iso(captured_at) if isinstance(captured_at, str) else captured_at
    if dt is None:
        return FreshnessState.UNKNOWN
    age = (now or utcnow()) - dt
    if age > EXPIRED_AFTER:
        return FreshnessState.EXPIRED
    if age > STALE_AFTER:
        return FreshnessState.STALE
    return FreshnessState.FRESH
