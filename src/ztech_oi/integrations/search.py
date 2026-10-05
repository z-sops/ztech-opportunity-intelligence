"""Web search abstraction used by competitor discovery and content discovery.

Vendor-neutral: Brave Search API and Serper (Google SERP) are supported out
of the box. With no key configured, `NullSearch` makes dependent providers
return an honest UNAVAILABLE / PROVIDER_NOT_CONFIGURED status.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.errors import FetchError
from ..domain.taxonomy import ErrorCode
from ..net.http import SafeHttpClient


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    snippet: str
    rank: int


class SearchClient(Protocol):
    name: str
    configured: bool

    async def search(self, query: str, *, count: int = 10, country: str | None = None) -> list[SearchResult]: ...


class NullSearch:
    name = "none"
    configured = False

    async def search(self, query: str, *, count: int = 10, country: str | None = None) -> list[SearchResult]:
        raise FetchError(ErrorCode.PROVIDER_NOT_CONFIGURED, "no search API configured (set BRAVE_API_KEY or SERPER_API_KEY)")


class BraveSearch:
    name = "brave"
    configured = True
    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, http: SafeHttpClient, api_key: str) -> None:
        self._http = http
        self._key = api_key

    async def search(self, query: str, *, count: int = 10, country: str | None = None) -> list[SearchResult]:
        params: dict[str, str | int] = {"q": query[:400], "count": max(1, min(count, 20))}
        if country:
            params["country"] = country
        resp = await self._http.get(
            self.ENDPOINT, params=params, headers={"X-Subscription-Token": self._key, "Accept": "application/json"}
        )
        if resp.status in (401, 403):
            raise FetchError(ErrorCode.PROVIDER_NOT_CONFIGURED, "Brave Search rejected the API key")
        if not resp.ok:
            raise FetchError(ErrorCode.SOURCE_UNAVAILABLE, f"Brave Search HTTP {resp.status}")
        data = resp.json()
        results = (data.get("web") or {}).get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return []
        out: list[SearchResult] = []
        for i, r in enumerate(results):
            if isinstance(r, dict) and isinstance(r.get("url"), str):
                out.append(
                    SearchResult(
                        url=r["url"], title=str(r.get("title", ""))[:300], snippet=str(r.get("description", ""))[:500], rank=i + 1
                    )
                )
        return out


class SerperSearch:
    name = "serper"
    configured = True
    ENDPOINT = "https://google.serper.dev/search"

    def __init__(self, http: SafeHttpClient, api_key: str) -> None:
        self._http = http
        self._key = api_key

    async def search(self, query: str, *, count: int = 10, country: str | None = None) -> list[SearchResult]:
        body: dict[str, str | int] = {"q": query[:400], "num": max(1, min(count, 20))}
        if country:
            body["gl"] = country.lower()
        resp = await self._http.post_json(self.ENDPOINT, body, headers={"X-API-KEY": self._key})
        if resp.status in (401, 403):
            raise FetchError(ErrorCode.PROVIDER_NOT_CONFIGURED, "Serper rejected the API key")
        if not resp.ok:
            raise FetchError(ErrorCode.SOURCE_UNAVAILABLE, f"Serper HTTP {resp.status}")
        data = resp.json()
        organic = data.get("organic") if isinstance(data, dict) else None
        if not isinstance(organic, list):
            return []
        out: list[SearchResult] = []
        for i, r in enumerate(organic):
            if isinstance(r, dict) and isinstance(r.get("link"), str):
                out.append(
                    SearchResult(
                        url=r["link"], title=str(r.get("title", ""))[:300], snippet=str(r.get("snippet", ""))[:500], rank=i + 1
                    )
                )
        return out
