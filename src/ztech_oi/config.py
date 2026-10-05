"""Runtime settings, loaded from environment variables only (no secrets in code).

Secrets are held in SecretStr-like private attributes and are never included
in reports, logs, MCP responses or `describe()`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)) or default)
    except ValueError:
        return default


@dataclass
class Limits:
    http_timeout_s: float = 12.0
    http_max_bytes: int = 3_000_000
    http_max_concurrency: int = 8
    http_per_host_interval_s: float = 0.5
    http_max_retries: int = 2
    provider_timeout_s: float = 60.0
    max_sitemap_urls: int = 2000
    max_sitemap_files: int = 5
    max_pages_fetched: int = 8
    max_inventory_items: int = 300
    max_competitors: int = 5
    max_search_queries: int = 4
    max_ads_per_entity: int = 50
    max_posts_per_entity: int = 50
    entity_concurrency: int = 3


@dataclass
class Settings:
    db_path: str = "ztech_oi.sqlite3"
    log_level: str = "INFO"
    search_provider: str | None = None
    meta_ad_countries: list[str] = field(default_factory=lambda: ["US"])
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    limits: Limits = field(default_factory=Limits)
    # secrets (private by convention; never serialised)
    _brave_key: str | None = None
    _serper_key: str | None = None
    _llm_key: str | None = None
    _meta_token: str | None = None
    _serpapi_key: str | None = None
    _x_bearer: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        lim = Limits(
            http_timeout_s=_float("ZTECH_OI_HTTP_TIMEOUT_S", 12.0),
            http_max_bytes=_int("ZTECH_OI_HTTP_MAX_BYTES", 3_000_000),
            http_max_concurrency=_int("ZTECH_OI_HTTP_MAX_CONCURRENCY", 8),
            http_per_host_interval_s=_float("ZTECH_OI_PER_HOST_INTERVAL_S", 0.5),
            provider_timeout_s=_float("ZTECH_OI_PROVIDER_TIMEOUT_S", 60.0),
            max_pages_fetched=_int("ZTECH_OI_MAX_PAGES", 8),
        )
        countries = [c.strip().upper() for c in (_env("META_AD_COUNTRIES", "US") or "US").split(",") if c.strip()]
        return cls(
            db_path=_env("ZTECH_OI_DB_PATH", "ztech_oi.sqlite3") or "ztech_oi.sqlite3",
            log_level=_env("ZTECH_OI_LOG_LEVEL", "INFO") or "INFO",
            search_provider=_env("ZTECH_OI_SEARCH_PROVIDER"),
            meta_ad_countries=countries,
            llm_base_url=_env("LLM_BASE_URL", "https://api.openai.com/v1") or "https://api.openai.com/v1",
            llm_model=_env("LLM_MODEL", "gpt-4o-mini") or "gpt-4o-mini",
            limits=lim,
            _brave_key=_env("BRAVE_API_KEY"),
            _serper_key=_env("SERPER_API_KEY"),
            _llm_key=_env("LLM_API_KEY") or _env("OPENAI_API_KEY"),
            _meta_token=_env("META_ACCESS_TOKEN"),
            _serpapi_key=_env("SERPAPI_API_KEY"),
            _x_bearer=_env("X_BEARER_TOKEN"),
        )

    def describe(self) -> dict[str, object]:
        """Safe, secret-free description of what is configured."""
        return {
            "db_path": os.path.basename(self.db_path),
            "search": "brave" if self._brave_key else ("serper" if self._serper_key else None),
            "llm": {"configured": bool(self._llm_key), "model": self.llm_model if self._llm_key else None},
            "meta_ad_library": {"configured": bool(self._meta_token), "countries": self.meta_ad_countries},
            "google_ads_transparency_via_serpapi": bool(self._serpapi_key),
            "x_api": bool(self._x_bearer),
            "linkedin": "unsupported",
        }

    def __repr__(self) -> str:  # never leak secrets via repr
        return f"Settings({self.describe()!r})"
