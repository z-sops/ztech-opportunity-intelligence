"""Security boundaries (spec #36) and the bounded HTTP layer."""

from __future__ import annotations

import gzip
import logging

import httpx
import pytest
from fakes import FakeWeb

from ztech_oi.domain.errors import FetchError, UnsafeTarget
from ztech_oi.domain.taxonomy import ErrorCode
from ztech_oi.net.http import SafeHttpClient
from ztech_oi.observability.logging import JsonFormatter, redact
from ztech_oi.security.url_guard import UrlGuard, is_public_ip, sanitize_url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://user:pw@example.com/",
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost/",
        "http://printer.local/",
        "http://metadata.google.internal/",
        "http://example.com:8080/",
        "http://exa mple.com/",
        "",
        "http://" + "a" * 2100 + ".com/",
        "http://no_tld/",
    ],
)
def test_sanitize_rejects_bad_urls(url):
    with pytest.raises(UnsafeTarget):
        sanitize_url(url)


def test_sanitize_accepts_public_and_strips_fragment():
    assert sanitize_url("https://Example.com/a?b=1#frag") == "https://example.com/a?b=1"


@pytest.mark.parametrize(
    "ip,ok",
    [
        ("93.184.216.34", True),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("fd00::1", False),
        ("::ffff:10.0.0.1", False),
        ("2606:4700::1111", True),
    ],
)
def test_public_ip_classification(ip, ok):
    assert is_public_ip(ip) is ok


async def test_dns_rebinding_to_private_is_blocked():
    async def resolver(host):
        return ["10.0.0.7"] if host == "evil.com" else ["93.184.216.34"]

    guard = UrlGuard(resolver=resolver)
    assert await guard.check("https://good.com/") == "https://good.com/"
    with pytest.raises(UnsafeTarget) as ei:
        await guard.check("https://evil.com/")
    assert ei.value.code is ErrorCode.SSRF_BLOCKED


async def test_dns_mixed_public_private_is_blocked():
    async def resolver(host):
        return ["93.184.216.34", "192.168.0.10"]

    with pytest.raises(UnsafeTarget):
        await UrlGuard(resolver=resolver).check("https://mixed.com/")


def _client(web: FakeWeb, **kw) -> SafeHttpClient:
    return SafeHttpClient(
        UrlGuard(resolver=web.resolver), transport=web.transport(), per_host_interval_s=0, backoff_base_s=0.0, **kw
    )


async def test_redirect_to_private_network_is_blocked(web):
    web.dns["internal.corp-example.com"] = ["10.0.0.9"]
    web.add("https://site.com/", "", status=302, headers={"location": "https://internal.corp-example.com/admin"})
    async with _client(web) as http:
        with pytest.raises(FetchError) as ei:
            await http.get("https://site.com/")
    assert ei.value.code is ErrorCode.SSRF_BLOCKED
    assert web.called("internal.corp-example.com") == 0


async def test_redirect_chain_is_bounded(web):
    for i in range(10):
        web.add(f"https://loop.com/{i}", "", status=302, headers={"location": f"https://loop.com/{i + 1}"})
    async with _client(web, max_redirects=3) as http:
        with pytest.raises(FetchError):
            await http.get("https://loop.com/0")


async def test_huge_response_is_cut_off(web):
    web.add("https://big.com/", "x" * 50_000)
    async with _client(web, max_bytes=10_000) as http:
        with pytest.raises(FetchError) as ei:
            await http.get("https://big.com/")
    assert ei.value.code is ErrorCode.RESPONSE_TOO_LARGE


async def test_declared_content_length_too_large(web):
    web.add_fn("https://big2.com/", lambda r: __import__("fakes").Route(200, b"x", {"content-length": "999999999"}))
    async with _client(web, max_bytes=1000) as http:
        with pytest.raises(FetchError) as ei:
            await http.get("https://big2.com/")
    assert ei.value.code is ErrorCode.RESPONSE_TOO_LARGE


async def test_rate_limit_retries_then_raises(web):
    web.add("https://busy.com/", "slow down", status=429, headers={"retry-after": "0"})
    async with _client(web, max_retries=2) as http:
        with pytest.raises(FetchError) as ei:
            await http.get("https://busy.com/")
    assert ei.value.code is ErrorCode.RATE_LIMITED and ei.value.retryable
    assert web.called("busy.com") == 3  # 1 + 2 retries, bounded


async def test_rate_limit_recovers(web):
    from fakes import Route

    seq = iter([Route(429, b"", {"retry-after": "0"}), Route(200, b"ok", {"content-type": "text/plain"})])
    web.add_fn("https://flaky.com/", lambda r: next(seq))
    async with _client(web) as http:
        r = await http.get("https://flaky.com/")
    assert r.ok and r.retries == 1


async def test_timeout_maps_to_timeout_code():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    web = FakeWeb()
    http = SafeHttpClient(
        UrlGuard(resolver=web.resolver),
        transport=httpx.MockTransport(boom),
        per_host_interval_s=0,
        backoff_base_s=0,
        max_retries=1,
    )
    with pytest.raises(FetchError) as ei:
        await http.get("https://slow.com/")
    assert ei.value.code is ErrorCode.TIMEOUT
    await http.aclose()


async def test_malformed_json_raises_malformed_response(web):
    web.add("https://api.example-json.com/x", "{not json", ctype="application/json")
    async with _client(web) as http:
        r = await http.get("https://api.example-json.com/x")
        with pytest.raises(FetchError) as ei:
            r.json()
    assert ei.value.code is ErrorCode.MALFORMED_RESPONSE


def test_log_redaction_of_secrets():
    assert redact({"access_token": "abc", "nested": {"api_key": "x"}, "ok": 1}) == {
        "access_token": "[REDACTED]",
        "nested": {"api_key": "[REDACTED]"},
        "ok": 1,
    }
    assert "SECRET" not in redact("GET https://g.com/x?access_token=SECRET&y=1")
    assert "tok123" not in redact("Authorization: Bearer tok123")
    rec = logging.makeLogRecord({"msg": "calling ?api_key=XYZ", "name": "ztech_oi", "levelname": "INFO"})
    assert "XYZ" not in JsonFormatter().format(rec)


def test_gzip_sitemap_and_bounds():
    from ztech_oi.net.parsing import maybe_gunzip

    data = gzip.compress(b"<urlset></urlset>")
    assert maybe_gunzip(data) == b"<urlset></urlset>"
    with pytest.raises(ValueError):
        maybe_gunzip(gzip.compress(b"x" * 2000), max_bytes=100)
