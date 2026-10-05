from __future__ import annotations

import pytest
from fakes import FakeLLM, FakeSearch, FakeWeb, build_site, sr

from ztech_oi.application.container import Engine, build_engine
from ztech_oi.config import Limits, Settings
from ztech_oi.persistence.memory import InMemoryRepository


def fast_settings(**secrets) -> Settings:
    s = Settings(db_path=":memory:", limits=Limits(http_per_host_interval_s=0.0, http_max_retries=1, provider_timeout_s=10))
    for k, v in secrets.items():
        setattr(s, f"_{k}", v)
    return s


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture
def market(web: FakeWeb) -> FakeWeb:
    """Prospect with little content vs two active competitors."""
    build_site(web, "sweetcrumb.com", name="SweetCrumb Bakery", articles_days=[45, 200], careers=False)
    build_site(
        web,
        "rivalbakes.com",
        name="Rival Bakes",
        articles_days=[2, 8, 15, 22, 30, 41, 55],
        landing=4,
        careers=True,
        offers="20% off wedding tastings this month",
    )
    build_site(web, "cakehouse.com", name="Cake House", articles_days=[5, 12, 19, 33, 50], landing=3)
    return web


@pytest.fixture
def search_market() -> FakeSearch:
    return FakeSearch(
        default=[
            sr("https://rivalbakes.com/", "Rival Bakes | Wedding Cakes Chicago", "Rival Bakes makes wedding cakes", 1),
            sr("https://www.yelp.com/biz/x", "Top 10 bakeries in Chicago - Yelp", "", 2),
            sr("https://cakehouse.com/", "Cake House - Custom cakes", "Cake House Chicago", 3),
            sr("https://sweetcrumb.com/", "SweetCrumb Bakery", "", 4),
        ]
    )


def make_engine(web: FakeWeb, *, search=None, llm=None, repo=None, settings: Settings | None = None) -> Engine:
    return build_engine(
        settings or fast_settings(),
        transport=web.transport(),
        resolver=web.resolver,
        search=search or FakeSearch(),
        llm=llm,
        repo=repo or InMemoryRepository(),
    )


PROSPECT = {
    "company_name": "SweetCrumb Bakery",
    "domain": "sweetcrumb.com",
    "location": "Chicago",
    "industry": "Bakery",
    "products_services": ["wedding cakes"],
}

__all__ = ["FakeLLM", "PROSPECT", "fast_settings", "make_engine"]
