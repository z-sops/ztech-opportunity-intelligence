"""Bounded, SSRF-safe HTTP client used by every provider (spec #36).

Guarantees:
  * every URL (including each redirect hop) passes UrlGuard
  * per-request timeout, global concurrency cap, per-host minimum interval
  * response bodies are streamed and cut off at `max_bytes` (RESPONSE_TOO_LARGE)
  * 429 / 5xx retried with bounded exponential backoff (Retry-After honoured, capped)
  * secrets are never logged: only scheme://host/path is ever written to logs

The underlying httpx transport is injectable so tests run fully offline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ..domain.errors import FetchError, UnsafeTarget
from ..domain.taxonomy import ErrorCode
from ..security.url_guard import UrlGuard

log = logging.getLogger("ztech_oi.http")

DEFAULT_UA = "ZTechOpportunityIntelligence/1.0 (+research bot; respects robots.txt)"


@dataclass
class FetchResponse:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    content: bytes
    elapsed_ms: int
    retries: int = 0
    redirects: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        ctype = self.headers.get("content-type", "")
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.content)
        except (ValueError, UnicodeDecodeError) as e:
            raise FetchError(ErrorCode.MALFORMED_RESPONSE, f"invalid JSON from {_safe(self.final_url)}") from e

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _safe(url: str) -> str:
    """Log-safe URL: drop query string and fragment (may contain tokens)."""
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.hostname}{p.path}"
    except ValueError:
        return "<invalid-url>"


class SafeHttpClient:
    def __init__(
        self,
        guard: UrlGuard,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 12.0,
        max_bytes: int = 3_000_000,
        max_redirects: int = 5,
        max_retries: int = 2,
        max_concurrency: int = 8,
        per_host_interval_s: float = 0.5,
        user_agent: str = DEFAULT_UA,
        backoff_base_s: float = 0.5,
        max_retry_after_s: float = 10.0,
    ) -> None:
        self.guard = guard
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
            headers={"User-Agent": user_agent, "Accept-Language": "en"},
        )
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.max_retry_after_s = max_retry_after_s
        self._sem = asyncio.Semaphore(max_concurrency)
        self._interval = per_host_interval_s
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SafeHttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- public
    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> FetchResponse:
        return await self.request("GET", url, params=params, headers=headers, follow_redirects=follow_redirects)

    async def post_json(self, url: str, body: dict[str, Any], *, headers: dict[str, str] | None = None) -> FetchResponse:
        return await self.request("POST", url, json_body=body, headers=headers, follow_redirects=False)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        follow_redirects: bool = True,
    ) -> FetchResponse:
        started = time.monotonic()
        current = await self.guard.check(url)
        redirects: list[str] = []
        retries_total = 0
        for _hop in range(self.max_redirects + 1):
            resp, retries = await self._send_with_retry(method, current, params, headers, json_body)
            retries_total += retries
            if follow_redirects and resp.status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    break
                nxt = urljoin(current, loc)
                try:
                    current = await self.guard.check(nxt)
                except UnsafeTarget as e:
                    raise FetchError(e.code, f"redirect to unsafe target blocked: {e.message}") from e
                redirects.append(current)
                params = None  # params already applied to the first hop
                if resp.status == 303:
                    method, json_body = "GET", None
                continue
            resp.url = url
            resp.final_url = current
            resp.retries = retries_total
            resp.redirects = redirects
            resp.elapsed_ms = int((time.monotonic() - started) * 1000)
            return resp
        raise FetchError(ErrorCode.SOURCE_UNAVAILABLE, f"too many redirects for {_safe(url)}")

    # --------------------------------------------------------------- internals
    async def _pace(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._host_last.get(host)
            if last is not None:
                wait = self._interval - (time.monotonic() - last)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._host_last[host] = time.monotonic()

    async def _send_with_retry(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        json_body: dict[str, Any] | None,
    ) -> tuple[FetchResponse, int]:
        attempt = 0
        while True:
            try:
                resp = await self._send_once(method, url, params, headers, json_body)
            except httpx.TimeoutException as e:
                if attempt < self.max_retries:
                    attempt += 1
                    await asyncio.sleep(self.backoff_base_s * 2 ** (attempt - 1))
                    continue
                raise FetchError(ErrorCode.TIMEOUT, f"timeout fetching {_safe(url)}", retryable=True) from e
            except httpx.TransportError as e:
                if attempt < self.max_retries:
                    attempt += 1
                    await asyncio.sleep(self.backoff_base_s * 2 ** (attempt - 1))
                    continue
                raise FetchError(
                    ErrorCode.SOURCE_UNAVAILABLE,
                    f"network error fetching {_safe(url)}: {type(e).__name__}",
                    retryable=True,
                ) from e
            if resp.status == 429 or resp.status >= 500:
                if attempt < self.max_retries:
                    attempt += 1
                    delay = self.backoff_base_s * 2 ** (attempt - 1)
                    ra = resp.headers.get("retry-after")
                    if ra and ra.isdigit():
                        delay = min(float(ra), self.max_retry_after_s)
                    log.info("retrying", extra={"url": _safe(url), "status": resp.status, "attempt": attempt})
                    await asyncio.sleep(delay)
                    continue
                if resp.status == 429:
                    raise FetchError(ErrorCode.RATE_LIMITED, f"rate limited by {_safe(url)}", retryable=True)
            return resp, attempt

    async def _send_once(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        json_body: dict[str, Any] | None,
    ) -> FetchResponse:
        host = urlsplit(url).hostname or ""
        await self._pace(host)
        async with self._sem:
            req = self._client.build_request(method, url, params=params, headers=headers, json=json_body)
            resp = await self._client.send(req, stream=True)
            try:
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.max_bytes:
                    raise FetchError(ErrorCode.RESPONSE_TOO_LARGE, f"response too large from {_safe(url)}")
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > self.max_bytes:
                        raise FetchError(ErrorCode.RESPONSE_TOO_LARGE, f"response too large from {_safe(url)}")
            finally:
                await resp.aclose()
        return FetchResponse(
            url=url,
            final_url=url,
            status=resp.status_code,
            headers={k.lower(): v for k, v in resp.headers.items()},
            content=bytes(buf),
            elapsed_ms=0,
        )
