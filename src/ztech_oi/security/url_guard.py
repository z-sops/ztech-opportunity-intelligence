"""URL sanitisation and SSRF protection (spec #36).

Rules:
  * only http/https, default ports 80/443 (or explicit 80/443)
  * no credentials in URLs, max length 2048
  * hostname must be a public DNS name (IP literals rejected)
  * every resolved address must be globally routable; private, loopback,
    link-local, CGNAT, multicast, reserved and metadata ranges are rejected
  * the check runs for the initial URL AND every redirect hop (see net.http)

DNS resolution is injectable so tests run deterministically offline.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from ..domain.errors import UnsafeTarget
from ..domain.identity import normalize_domain
from ..domain.taxonomy import ErrorCode

Resolver = Callable[[str], Awaitable[list[str]]]

MAX_URL_LENGTH = 2048
_BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet")
_METADATA_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def is_public_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in ipaddress.ip_network("100.64.0.0/10"):  # CGNAT
        return False
    return ip.is_global and not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def sanitize_url(raw: str) -> str:
    """Syntactic validation; returns a normalised URL string or raises UnsafeTarget."""
    if not isinstance(raw, str) or not raw.strip():
        raise UnsafeTarget(ErrorCode.INVALID_URL, "empty URL")
    raw = raw.strip()
    if len(raw) > MAX_URL_LENGTH:
        raise UnsafeTarget(ErrorCode.INVALID_URL, "URL too long")
    if any(ch in raw for ch in ("\x00", "\r", "\n", "\t", " ")):
        raise UnsafeTarget(ErrorCode.INVALID_URL, "URL contains control/whitespace characters")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as e:
        raise UnsafeTarget(ErrorCode.INVALID_URL, f"unparseable URL: {e}") from e
    if parts.scheme not in ("http", "https"):
        raise UnsafeTarget(ErrorCode.INVALID_URL, f"scheme '{parts.scheme}' not allowed")
    if parts.username or parts.password:
        raise UnsafeTarget(ErrorCode.INVALID_URL, "credentials in URL are not allowed")
    if port not in (None, 80, 443):
        raise UnsafeTarget(ErrorCode.SSRF_BLOCKED, f"port {port} not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeTarget(ErrorCode.INVALID_URL, "missing host")
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise UnsafeTarget(ErrorCode.SSRF_BLOCKED, "IP-literal hosts are not allowed")
    except ValueError:
        pass
    if host in _METADATA_HOSTS or host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise UnsafeTarget(ErrorCode.SSRF_BLOCKED, f"host '{host}' is internal")
    bare = host[4:] if host.startswith("www.") else host
    if not normalize_domain(bare):
        raise UnsafeTarget(ErrorCode.INVALID_URL, f"host '{host}' is not a valid public hostname")
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


class UrlGuard:
    """Async guard combining syntactic checks with DNS-resolution checks."""

    def __init__(self, resolver: Resolver | None = None, allow_hosts: set[str] | None = None) -> None:
        self._resolver = resolver or system_resolver
        self._cache: dict[str, bool] = {}
        # Hosts of well-known public APIs we call (skip DNS round trip, still syntactically checked).
        self._allow_hosts = allow_hosts or set()

    async def check(self, raw: str) -> str:
        url = sanitize_url(raw)
        host = urlsplit(url).hostname or ""
        if host in self._allow_hosts:
            return url
        ok = self._cache.get(host)
        if ok is None:
            try:
                addrs = await self._resolver(host)
            except (OSError, socket.gaierror) as e:
                raise UnsafeTarget(ErrorCode.INVALID_DOMAIN, f"DNS resolution failed for '{host}'") from e
            if not addrs:
                raise UnsafeTarget(ErrorCode.INVALID_DOMAIN, f"no DNS records for '{host}'")
            ok = all(is_public_ip(a) for a in addrs)
            self._cache[host] = ok
        if not ok:
            raise UnsafeTarget(ErrorCode.SSRF_BLOCKED, f"host '{host}' resolves to a non-public address")
        return url
