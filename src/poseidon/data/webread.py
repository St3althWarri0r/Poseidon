"""SSRF-guarded web fetch — the only door between ``read_url`` and the network.

Every hop of every fetch passes the FULL guard before any I/O:

  * scheme allowlist (``https``; ``http`` only when the operator opts in);
  * no userinfo in the URL, port 80/443/default only;
  * the host must not be — literally or via ANY resolved A/AAAA record
    (IPv4-mapped IPv6 unwrapped) — private, loopback, link-local, multicast,
    reserved, unspecified, or RFC6598 CGNAT (100.64.0.0/10);
  * redirects are never auto-followed: each ``Location`` is re-validated from
    scratch (a public host cannot bounce the fetch into the metadata service);
  * the body is streamed and aborted past ``max_bytes``; only text-ish content
    types are read (no OCR / document parsing — binaries are refused);
  * fixed User-Agent, no cookies, no environment proxies/credentials.

The resolver and transport are injectable so tests exercise the real guard
offline. Residual limit (documented, accepted): validation resolves DNS and
httpx then re-resolves to connect, so a fast-flipping rebinder could in theory
race the two lookups — the guard is a strong filter, not a pinned socket.

Raises :class:`WebReadBlockedError` for policy refusals and
:class:`WebReadError` for operational failures (network/HTTP/size/redirects);
both are :class:`DataError` subclasses so the tool loop reports an honest
data gap instead of trading on a half-read page.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import SplitResult, urljoin, urlsplit

import httpx
import structlog

from ..core.config import WebReadConfig
from ..core.errors import WebReadBlockedError, WebReadError

log = structlog.get_logger(__name__)

Resolver = Callable[[str], Awaitable[list[str]]]

_USER_AGENT = "Poseidon-Research/1.0"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_PORTS = frozenset({80, 443})
# Text-ish content the extractor can honestly represent. Binaries (PDF, images,
# archives) are refused rather than parsed — no OCR / document reader here.
_ALLOWED_MEDIA_EXACT = frozenset({"application/json", "application/xhtml+xml"})
_HTML_MEDIA = frozenset({"text/html", "application/xhtml+xml"})


@dataclass(frozen=True)
class FetchResult:
    """The extracted-text view of one guarded fetch."""

    final_url: str
    host: str
    status: int
    content_type: str
    title: str | None
    text: str
    total_chars: int


async def _default_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # RFC6598 carrier-grade NAT


def _address_disallowed(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:  # ::ffff:10.0.0.5 is 10.0.0.5 in a v6 coat
        addr = mapped
    # CGNAT is checked explicitly: it is neither private nor reserved to
    # ``ipaddress``, yet it addresses the carrier's own infrastructure. Note we
    # deliberately do NOT collapse this to ``not addr.is_global`` — multicast
    # (224.0.0.1) reports is_global=True, so that swap would open a hole.
    if isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT:
        return True
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified)


def _validate_static(url: str, cfg: WebReadConfig) -> SplitResult:
    """Scheme / userinfo / port / host-shape policy — no I/O."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    allowed = {"https", "http"} if cfg.allow_http else {"https"}
    if scheme not in allowed:
        raise WebReadBlockedError(f"blocked {url!r}: scheme {scheme or '?'!r} is not allowed")
    if parts.username is not None or parts.password is not None:
        raise WebReadBlockedError(f"blocked {url!r}: userinfo in URLs is not allowed")
    if not parts.hostname:
        raise WebReadBlockedError(f"blocked {url!r}: no host")
    try:
        port = parts.port  # property parse can raise on out-of-range ports
    except ValueError as exc:
        raise WebReadBlockedError(f"blocked {url!r}: invalid port") from exc
    if port is not None and port not in _ALLOWED_PORTS:
        raise WebReadBlockedError(f"blocked {url!r}: port {port} is not allowed")
    return parts


async def _validate_host(host: str, url: str, resolve: Resolver) -> None:
    """Refuse hosts that are — literally or via ANY resolved record — outside
    public address space. Runs BEFORE the request on every hop."""
    try:
        literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _address_disallowed(literal):
            raise WebReadBlockedError(f"blocked {url!r}: {host} is not a public address")
        return
    try:
        records = await resolve(host)
    except WebReadError:
        raise
    except Exception as exc:
        raise WebReadError(f"DNS resolution failed for {host!r}: {exc}") from exc
    if not records:
        raise WebReadError(f"DNS returned no addresses for {host!r}")
    for record in records:
        try:
            addr = ipaddress.ip_address(record)
        except ValueError as exc:
            raise WebReadError(f"unparseable DNS record {record!r} for {host!r}") from exc
        if _address_disallowed(addr):
            raise WebReadBlockedError(
                f"blocked {url!r}: {host} resolves to non-public address {record}")


def _media_allowed(media: str) -> bool:
    return media.startswith("text/") or media in _ALLOWED_MEDIA_EXACT


def _charset_from(content_type: str) -> str | None:
    for param in content_type.split(";")[1:]:
        key, _, value = param.strip().partition("=")
        if key.strip().lower() == "charset" and value:
            return value.strip().strip('"').strip("'")
    return None


class _HTMLTextExtractor(HTMLParser):
    """Plain-text + title extraction: script/style/noscript/template dropped,
    block boundaries become newlines, inline markup keeps text flowing."""

    _SKIP = frozenset({"script", "style", "noscript", "template"})
    _BLOCK = frozenset({
        "address", "article", "aside", "blockquote", "br", "caption", "dd",
        "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
        "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
        "main", "nav", "ol", "p", "pre", "section", "table", "td", "th",
        "tr", "ul",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._title: list[str] = []
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        if tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        if tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)
        elif self._skip_depth == 0:
            self._chunks.append(data)

    def result(self) -> tuple[str | None, str]:
        title = " ".join("".join(self._title).split()) or None
        lines = [" ".join(line.split()) for line in "".join(self._chunks).split("\n")]
        return title, "\n".join(line for line in lines if line)


def _extract(media: str, raw_text: str) -> tuple[str | None, str]:
    if media in _HTML_MEDIA:
        parser = _HTMLTextExtractor()
        parser.feed(raw_text)
        parser.close()
        return parser.result()
    return None, raw_text.strip()


async def guarded_fetch(url: str, cfg: WebReadConfig, *,
                        resolver: Resolver | None = None,
                        transport: httpx.AsyncBaseTransport | None = None) -> FetchResult:
    """Fetch ``url`` through the full SSRF guard and return its extracted text.

    ``resolver``/``transport`` are injectable for tests; production uses the
    event loop's ``getaddrinfo`` and a plain httpx transport.
    """
    resolve = resolver or _default_resolver
    current = url
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False, trust_env=False,
        timeout=cfg.timeout_seconds, headers={"User-Agent": _USER_AGENT},
    ) as client:
        for _hop in range(cfg.max_redirects + 1):
            parts = _validate_static(current, cfg)
            host = (parts.hostname or "").lower()
            await _validate_host(host, current, resolve)
            try:
                async with client.stream("GET", current) as response:
                    status = response.status_code
                    if status in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise WebReadError(f"HTTP {status} without Location from {current!r}")
                        current = urljoin(current, location)
                        continue
                    if not 200 <= status < 300:
                        raise WebReadError(f"HTTP {status} from {current!r}")
                    content_type = response.headers.get("content-type", "")
                    media = content_type.split(";")[0].strip().lower()
                    if not _media_allowed(media):
                        raise WebReadBlockedError(
                            f"blocked {current!r}: content-type {media or '?'!r} is not "
                            "readable text (binaries/documents are not fetched)")
                    received = 0
                    chunks: list[bytes] = []
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > cfg.max_bytes:
                            raise WebReadError(
                                f"response from {current!r} exceeded max_bytes "
                                f"({cfg.max_bytes}); fetch aborted")
                        chunks.append(chunk)
            except httpx.HTTPError as exc:
                raise WebReadError(f"fetch failed for {current!r}: {exc}") from exc
            charset = _charset_from(content_type) or "utf-8"
            body = b"".join(chunks)
            try:
                raw_text = body.decode(charset, errors="replace")
            except LookupError:  # unknown charset label: honest utf-8 fallback
                raw_text = body.decode("utf-8", errors="replace")
            title, text = _extract(media, raw_text)
            log.info("web read", host=host, status=status, media=media, chars=len(text))
            return FetchResult(final_url=current, host=host, status=status,
                               content_type=media, title=title, text=text,
                               total_chars=len(text))
    raise WebReadError(f"too many redirects (more than {cfg.max_redirects}) for {url!r}")
