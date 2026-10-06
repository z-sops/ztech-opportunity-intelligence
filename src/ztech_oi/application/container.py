"""Composition root: builds the service graph from Settings.

Tests (and future embedders) inject an httpx transport, DNS resolver, search,
LLM or repository; production uses the defaults derived from environment.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import Settings
from ..integrations.llm import LLMClient, NullLLM, OpenAICompatibleLLM
from ..integrations.search import BraveSearch, NullSearch, SearchClient, SerperSearch
from ..net.http import SafeHttpClient
from ..observability.logging import silence_third_party_url_logging
from ..persistence.repository import Repository
from ..persistence.sqlite import SQLiteRepository
from ..providers.ads import GoogleAdsProvider, MetaAdsProvider
from ..providers.base import IntelligenceProvider
from ..providers.competitors import CompetitorDiscoveryProvider
from ..providers.content import ContentProvider
from ..providers.social import LinkedInProvider, TwitterProvider
from ..providers.website import WebsiteProvider
from ..security.url_guard import Resolver, UrlGuard
from .research_service import ResearchService

# Fixed public API hosts: still syntactically validated, DNS check skipped.
API_HOSTS = {
    "api.search.brave.com",
    "google.serper.dev",
    "graph.facebook.com",
    "serpapi.com",
    "api.x.com",
    "api.openai.com",
}


@dataclass
class Engine:
    service: ResearchService
    http: SafeHttpClient
    repo: Repository
    settings: Settings

    async def aclose(self) -> None:
        await self.http.aclose()
        self.repo.close()


def build_engine(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
    search: SearchClient | None = None,
    llm: LLMClient | None = None,
    repo: Repository | None = None,
) -> Engine:
    s = settings or Settings.from_env()
    silence_third_party_url_logging()
    lim = s.limits
    from urllib.parse import urlsplit

    llm_host = urlsplit(s.llm_base_url).hostname or ""
    guard = UrlGuard(resolver=resolver, allow_hosts=API_HOSTS | ({llm_host} if llm_host else set()))
    http = SafeHttpClient(
        guard,
        transport=transport,
        timeout_s=lim.http_timeout_s,
        max_bytes=lim.http_max_bytes,
        max_retries=lim.http_max_retries,
        max_concurrency=lim.http_max_concurrency,
        per_host_interval_s=lim.http_per_host_interval_s,
    )
    if search is None:
        pref = (s.search_provider or "").lower()
        if s._brave_key and pref in ("", "brave"):
            search = BraveSearch(http, s._brave_key)
        elif s._serper_key:
            search = SerperSearch(http, s._serper_key)
        else:
            search = NullSearch()
    if llm is None:
        llm = (
            OpenAICompatibleLLM(http, api_key=s._llm_key, base_url=s.llm_base_url, model=s.llm_model) if s._llm_key else NullLLM()
        )
    repo = repo or SQLiteRepository(s.db_path)
    entity_providers: list[IntelligenceProvider] = [
        WebsiteProvider(
            http,
            max_pages=lim.max_pages_fetched,
            max_sitemap_urls=lim.max_sitemap_urls,
            max_sitemap_files=lim.max_sitemap_files,
            max_inventory_items=lim.max_inventory_items,
        ),
        ContentProvider(http, llm),
        MetaAdsProvider(http, s._meta_token, s.meta_ad_countries, max_ads=lim.max_ads_per_entity),
        GoogleAdsProvider(http, s._serpapi_key, max_ads=lim.max_ads_per_entity),
        TwitterProvider(http, s._x_bearer, max_posts=lim.max_posts_per_entity),
        LinkedInProvider(),
    ]
    discovery = CompetitorDiscoveryProvider(search, llm, max_queries=lim.max_search_queries)
    desc = s.describe()
    desc["search"] = search.name if search.configured else None
    desc["llm"] = {"configured": llm.configured, "model": s.llm_model if llm.configured else None}
    service = ResearchService(
        repo,
        entity_providers,
        discovery,
        provider_timeout_s=lim.provider_timeout_s,
        entity_concurrency=lim.entity_concurrency,
        config_description=desc,
    )
    service.recover_interrupted_jobs()
    return Engine(service=service, http=http, repo=repo, settings=s)
