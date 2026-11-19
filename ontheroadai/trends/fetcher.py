"""Polite HTTP fetching for feeds.

- Checks robots.txt before fetching (RFC 9309: a missing robots.txt means allowed; a server
  error or no answer means not allowed).
- Sends a clear User-Agent and uses conditional requests (ETag / Last-Modified), so unchanged
  feeds cost almost nothing.
- Waits at least `min_host_interval` seconds between requests to the same host.
- Never logs in, never sends cookies, and never retries through a 401/403/429.
- Caps response size and time.
The transport is pluggable so tests can run without the internet.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .. import __version__

USER_AGENT = f"OnTheRoadAI-TrendScout/{__version__} (personal research tool; reads public RSS/Atom feeds)"
MAX_BYTES = 3 * 1024 * 1024
TIMEOUT = 20


class TransportError(Exception):
    """Network-level failure: DNS, refused connection, blocked by a proxy, timeout."""


@dataclass
class Response:
    status: int
    headers: dict = field(default_factory=dict)  # lower-case keys
    body: bytes = b""


class UrllibTransport:
    """Real HTTP using only the standard library."""

    def get(self, url: str, headers: dict, timeout: float = TIMEOUT) -> Response:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (http/https only, checked)
                body = resp.read(MAX_BYTES + 1)
                return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, body)
        except urllib.error.HTTPError as exc:
            body = exc.read(64 * 1024) if exc.fp else b""
            return Response(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TransportError(str(reason)) from None


@dataclass
class FetchResult:
    status: str        # ok | not_modified | robots_disallowed | needs_credentials | rate_limited | http_error | network_error | too_large
    http_status: int | None = None
    body: bytes = b""
    etag: str | None = None
    last_modified: str | None = None
    error: str = ""
    retry_after: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "not_modified")


class Fetcher:
    DEFAULT_HOST_INTERVAL = 2.0  # seconds between requests to the same host

    def __init__(self, transport=None, user_agent: str = USER_AGENT, min_host_interval: float | None = None,
                 respect_robots: bool = True, sleep=time.sleep, clock=time.monotonic):
        self.transport = transport or UrllibTransport()
        self.user_agent = user_agent
        self.min_host_interval = self.DEFAULT_HOST_INTERVAL if min_host_interval is None else min_host_interval
        self.respect_robots = respect_robots
        self.sleep, self.clock = sleep, clock
        self._robots: dict[str, urllib.robotparser.RobotFileParser | bool] = {}
        self._last_hit: dict[str, float] = {}

    def _wait_for_host(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.min_host_interval - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_hit[host] = self.clock()

    def _get(self, url: str, headers: dict) -> Response:
        host = urlparse(url).netloc.lower()
        self._wait_for_host(host)
        return self.transport.get(url, {"User-Agent": self.user_agent, **headers}, TIMEOUT)

    def robots_allows(self, url: str) -> tuple[bool, str]:
        if not self.respect_robots:
            return True, ""
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        cached = self._robots.get(base)
        if cached is None:
            try:
                resp = self._get(base + "/robots.txt", {"Accept": "text/plain"})
            except TransportError as exc:
                self._robots[base] = False
                return False, f"robots.txt unreachable ({exc}); not fetching"
            if resp.status >= 500:
                self._robots[base] = False
                return False, f"robots.txt returned HTTP {resp.status}; not fetching"
            if 400 <= resp.status < 500:
                self._robots[base] = True  # RFC 9309: unavailable robots.txt -> allowed
                return True, ""
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(resp.body.decode("utf-8", "replace").splitlines())
            self._robots[base] = rp
            cached = rp
        if cached is True:
            return True, ""
        if cached is False:
            return False, "robots.txt could not be read; not fetching"
        if cached.can_fetch(self.user_agent, url):
            return True, ""
        return False, "robots.txt does not allow automated access to this URL"

    def fetch(self, url: str, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return FetchResult("http_error", error="Only http(s) URLs can be fetched")
        allowed, why = self.robots_allows(url)
        if not allowed:
            return FetchResult("robots_disallowed", error=why)
        headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            resp = self._get(url, headers)
        except TransportError as exc:
            return FetchResult("network_error", error=f"Could not connect: {exc}")
        h = resp.headers
        if resp.status == 304:
            return FetchResult("not_modified", 304, etag=etag, last_modified=last_modified)
        if resp.status in (401, 403):
            return FetchResult("needs_credentials", resp.status,
                               error=f"HTTP {resp.status}: the site refused access. It may require a login or "
                                     "block automated readers. Not retried.")
        if resp.status == 429:
            return FetchResult("rate_limited", 429, error="HTTP 429: the site asked us to slow down. Will retry later.",
                               retry_after=h.get("retry-after"))
        if resp.status >= 400:
            return FetchResult("http_error", resp.status, error=f"HTTP {resp.status}")
        if len(resp.body) > MAX_BYTES:
            return FetchResult("too_large", resp.status, error="Feed is larger than 3 MB; skipped")
        return FetchResult("ok", resp.status, resp.body, h.get("etag"), h.get("last-modified"))
