"""SSRF guard pins for ``data/webread.py`` (rank 6 red-first).

``guarded_fetch`` is the ONLY door between the PM's ``read_url`` tool and the
network, so every hop is validated: scheme allowlist, no userinfo, port
80/443/default only, and no request may ever reach a private/loopback/
link-local/multicast/reserved/unspecified address — whether written literally,
resolved via DNS (injected resolver), or smuggled behind a redirect. All tests
run offline: ``httpx.MockTransport`` serves responses and a tripwire transport
proves policy rejects happen before any I/O.
"""

from __future__ import annotations

import httpx
import pytest

from poseidon.core.config import WebReadConfig
from poseidon.core.errors import WebReadBlockedError, WebReadError
from poseidon.data.webread import guarded_fetch

_PUBLIC_A = "93.184.216.34"  # a genuinely global unicast address
_PUBLIC_B = "34.117.59.81"


def _tripwire_transport() -> httpx.MockTransport:
    """A transport that fails the test if any request is actually issued."""

    def _handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network I/O: {request.url}")

    return httpx.MockTransport(_handler)


def _resolver(mapping: dict[str, list[str]]):
    """Fake DNS: host -> list of resolved address strings."""

    async def resolve(host: str) -> list[str]:
        if host not in mapping:
            raise OSError(f"no fake DNS entry for {host}")
        return mapping[host]

    return resolve


def _forbid_resolver():
    async def resolve(host: str) -> list[str]:
        raise AssertionError(f"unexpected DNS resolution for {host}")

    return resolve


# ------------------------------------------------------------- static policy


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/pub/x.txt",
    "data:text/html,hello",
    "javascript:alert(1)",
    "gopher://example.com/1",
])
async def test_non_http_schemes_rejected(url: str) -> None:
    with pytest.raises(WebReadBlockedError):
        await guarded_fetch(url, WebReadConfig(), resolver=_forbid_resolver(),
                            transport=_tripwire_transport())


async def test_http_rejected_by_default() -> None:
    with pytest.raises(WebReadBlockedError):
        await guarded_fetch("http://example.com/", WebReadConfig(),
                            resolver=_forbid_resolver(), transport=_tripwire_transport())


async def test_userinfo_url_rejected() -> None:
    with pytest.raises(WebReadBlockedError):
        await guarded_fetch("https://user:pass@example.com/", WebReadConfig(),
                            resolver=_forbid_resolver(), transport=_tripwire_transport())


async def test_non_default_port_rejected() -> None:
    with pytest.raises(WebReadBlockedError):
        await guarded_fetch("https://example.com:8080/", WebReadConfig(),
                            resolver=_forbid_resolver(), transport=_tripwire_transport())


async def test_explicit_default_ports_allowed() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(
        200, content=b"ok", headers={"content-type": "text/plain"}))
    result = await guarded_fetch(
        "https://example.com:443/x", WebReadConfig(),
        resolver=_resolver({"example.com": [_PUBLIC_A]}), transport=transport)
    assert result.status == 200
    assert result.text == "ok"


# ------------------------------------------- literal non-public hosts (no I/O)


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/",
    "https://10.0.0.8/",
    "https://192.168.1.1/",
    "https://169.254.169.254/latest/meta-data",
    "https://[::1]/",
    "https://0.0.0.0/",
    "https://[::ffff:10.0.0.5]/",  # IPv4-mapped IPv6 smuggling a private v4
    "https://224.0.0.1/",  # multicast
    "https://100.64.0.1/",  # RFC6598 CGNAT — carrier-grade NAT space
    "https://100.127.255.254/",  # last CGNAT address
    "https://[::ffff:100.64.0.1]/",  # CGNAT smuggled as IPv4-mapped IPv6
])
async def test_literal_non_public_hosts_rejected_without_io(url: str) -> None:
    with pytest.raises(WebReadBlockedError):
        await guarded_fetch(url, WebReadConfig(), resolver=_forbid_resolver(),
                            transport=_tripwire_transport())


async def test_cgnat_neighbours_stay_reachable() -> None:
    # The block must be exactly 100.64.0.0/10 — the addresses either side of it
    # are ordinary global unicast and must not become collateral damage.
    transport = httpx.MockTransport(lambda _req: httpx.Response(
        200, content=b"ok", headers={"content-type": "text/plain"}))
    for host in ("100.63.255.255", "100.128.0.0"):
        result = await guarded_fetch(f"https://{host}/", WebReadConfig(),
                                     resolver=_forbid_resolver(), transport=transport)
        assert result.status == 200


# -------------------------------------------------------- DNS resolve-then-reject


async def test_dns_resolved_cgnat_rejected() -> None:
    # CGNAT is invisible to every ``ipaddress`` predicate the guard already
    # uses — is_private/is_reserved/is_multicast are all False for 100.64/10 —
    # so a name resolving into it would otherwise sail straight through.
    with pytest.raises(WebReadBlockedError):
        await guarded_fetch("https://example.com/", WebReadConfig(),
                            resolver=_resolver({"example.com": ["100.64.12.9"]}),
                            transport=_tripwire_transport())


async def test_dns_resolved_private_rejected() -> None:
    # The transport would happily serve the request — the guard must refuse
    # first because the name resolves into private space.
    transport = httpx.MockTransport(lambda _req: httpx.Response(
        200, content=b"gotcha", headers={"content-type": "text/plain"}))
    with pytest.raises(WebReadBlockedError):
        await guarded_fetch("https://example.com/", WebReadConfig(),
                            resolver=_resolver({"example.com": ["10.0.0.5"]}),
                            transport=transport)


async def test_dns_any_bad_record_rejects_whole_host() -> None:
    # One public A record does not launder a private one (rebinding hygiene:
    # ANY disallowed record refuses the host).
    with pytest.raises(WebReadBlockedError):
        await guarded_fetch(
            "https://example.com/", WebReadConfig(),
            resolver=_resolver({"example.com": [_PUBLIC_A, "192.168.7.7"]}),
            transport=_tripwire_transport())


async def test_dns_failure_is_webread_error() -> None:
    with pytest.raises(WebReadError):
        await guarded_fetch("https://no-such-host.example/", WebReadConfig(),
                            resolver=_resolver({}), transport=_tripwire_transport())


# ------------------------------------------------------------------- redirects


async def test_redirect_to_private_rejected() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ok.example":
            return httpx.Response(302, headers={"location": "https://127.0.0.1/x"})
        raise AssertionError(f"reached {request.url}")

    with pytest.raises(WebReadBlockedError):
        await guarded_fetch("https://ok.example/", WebReadConfig(),
                            resolver=_resolver({"ok.example": [_PUBLIC_A]}),
                            transport=httpx.MockTransport(_handler))


async def test_redirect_chain_within_limit_revalidates_and_lands() -> None:
    seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "a.example" and request.url.path == "/hop":
            return httpx.Response(301, headers={"location": "https://b.example/final"})
        if request.url.host == "a.example":
            return httpx.Response(302, headers={"location": "/hop"})  # relative
        if request.url.host == "b.example":
            return httpx.Response(200, content=b"landed",
                                  headers={"content-type": "text/plain"})
        raise AssertionError(f"reached {request.url}")

    result = await guarded_fetch(
        "https://a.example/", WebReadConfig(max_redirects=3),
        resolver=_resolver({"a.example": [_PUBLIC_A], "b.example": [_PUBLIC_B]}),
        transport=httpx.MockTransport(_handler))
    assert result.text == "landed"
    assert result.final_url == "https://b.example/final"
    assert result.host == "b.example"
    # The relative Location was joined against the CURRENT hop, not the origin.
    assert seen == ["https://a.example/", "https://a.example/hop", "https://b.example/final"]


async def test_redirect_chain_over_limit_rejected() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.strip("/") or 0)
        return httpx.Response(302, headers={"location": f"https://ok.example/{n + 1}"})

    with pytest.raises(WebReadError, match="redirect"):
        await guarded_fetch("https://ok.example/0", WebReadConfig(max_redirects=2),
                            resolver=_resolver({"ok.example": [_PUBLIC_A]}),
                            transport=httpx.MockTransport(_handler))


# --------------------------------------------------------------- body handling


async def test_size_cap_aborts_fetch() -> None:
    big = b"x" * 10_001
    transport = httpx.MockTransport(lambda _req: httpx.Response(
        200, content=big, headers={"content-type": "text/plain"}))
    with pytest.raises(WebReadError, match="max_bytes"):
        await guarded_fetch("https://example.com/big", WebReadConfig(max_bytes=10_000),
                            resolver=_resolver({"example.com": [_PUBLIC_A]}),
                            transport=transport)


async def test_http_error_status_raises() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(
        404, content=b"nope", headers={"content-type": "text/plain"}))
    with pytest.raises(WebReadError, match="HTTP 404"):
        await guarded_fetch("https://example.com/missing", WebReadConfig(),
                            resolver=_resolver({"example.com": [_PUBLIC_A]}),
                            transport=transport)


@pytest.mark.parametrize("content_type", ["application/pdf", "application/octet-stream",
                                          "image/png"])
async def test_binary_content_types_rejected(content_type: str) -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(
        200, content=b"\x00\x01", headers={"content-type": content_type}))
    with pytest.raises(WebReadBlockedError, match="content-type"):
        await guarded_fetch("https://example.com/doc", WebReadConfig(),
                            resolver=_resolver({"example.com": [_PUBLIC_A]}),
                            transport=transport)


async def test_html_extraction_strips_script_style_and_captures_title() -> None:
    html = (
        "<html><head><title>Example  Domain</title>"
        "<style>body { color: red; }</style>"
        "<script>alert('evil');</script></head>"
        "<body><h1>Heading</h1><p>First   paragraph.</p>"
        "<noscript>enable js</noscript>"
        "<div>Second <b>bold</b> line.</div></body></html>"
    )
    transport = httpx.MockTransport(lambda _req: httpx.Response(
        200, content=html.encode(), headers={"content-type": "text/html; charset=utf-8"}))
    result = await guarded_fetch("https://example.com/", WebReadConfig(),
                                 resolver=_resolver({"example.com": [_PUBLIC_A]}),
                                 transport=transport)
    assert result.title == "Example Domain"
    assert "alert(" not in result.text
    assert "color: red" not in result.text
    assert "enable js" not in result.text
    assert "Heading" in result.text
    assert "First paragraph." in result.text  # whitespace collapsed
    assert "Second bold line." in result.text  # inline tags keep flowing text
    assert result.text.index("Heading") < result.text.index("First paragraph.")
    assert result.total_chars == len(result.text)
    assert result.content_type == "text/html"


async def test_json_body_returned_verbatim() -> None:
    body = b'{"a": 1, "b": [2, 3]}'
    transport = httpx.MockTransport(lambda _req: httpx.Response(
        200, content=body, headers={"content-type": "application/json"}))
    result = await guarded_fetch("https://api.example.com/x", WebReadConfig(),
                                 resolver=_resolver({"api.example.com": [_PUBLIC_A]}),
                                 transport=transport)
    assert result.text == '{"a": 1, "b": [2, 3]}'
    assert result.title is None


async def test_charset_header_honored() -> None:
    body = "prix café".encode("latin-1")
    transport = httpx.MockTransport(lambda _req: httpx.Response(
        200, content=body, headers={"content-type": "text/plain; charset=iso-8859-1"}))
    result = await guarded_fetch("https://example.com/", WebReadConfig(),
                                 resolver=_resolver({"example.com": [_PUBLIC_A]}),
                                 transport=transport)
    assert result.text == "prix café"


async def test_allow_http_fetches_but_still_validates_ip() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(
        200, content=b"plain ok", headers={"content-type": "text/plain"}))
    cfg = WebReadConfig(allow_http=True)
    result = await guarded_fetch("http://example.com/", cfg,
                                 resolver=_resolver({"example.com": [_PUBLIC_A]}),
                                 transport=transport)
    assert result.text == "plain ok"
    # …but a private target is still refused even with http allowed.
    with pytest.raises(WebReadBlockedError):
        await guarded_fetch("http://192.168.1.1/", cfg, resolver=_forbid_resolver(),
                            transport=_tripwire_transport())
