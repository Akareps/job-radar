"""Polite HTTP client: per-host rate limiting, retries with backoff, honest UA.

Every source goes through this. The per-host throttle is what keeps us from
looking like a scraper to LinkedIn and jobs.ch.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import SSLError

log = logging.getLogger("jobradar.http")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Minimum seconds between requests to the same host. ATS APIs are built to be
# polled and get a light touch; the two we are scraping get a heavy one.
HOST_DELAYS = {
    "www.linkedin.com": 4.0,
    "linkedin.com": 4.0,
    "www.jobs.ch": 2.0,
    "jobs.ch": 2.0,
    "www.myscience.ch": 2.0,
    "_default": 0.35,
}

RETRY_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    """Serializes requests per host so concurrent sources cannot gang up on one."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(host, threading.Lock())

    def wait(self, host: str) -> None:
        delay = HOST_DELAYS.get(host, HOST_DELAYS["_default"])
        with self._lock_for(host):
            elapsed = time.monotonic() - self._last.get(host, 0.0)
            if elapsed < delay:
                # Jitter avoids a metronome-regular request pattern.
                time.sleep(delay - elapsed + random.uniform(0, 0.25))
            self._last[host] = time.monotonic()


_limiter = RateLimiter()


#: Exhausted timeouts before a host is dropped for the rest of the run.
TIMEOUT_STRIKES = 2


class Http:
    def __init__(self, timeout: int = 25, retries: int = 2, user_agent: str = DEFAULT_UA):
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "en-US,en;q=0.9,de;q=0.6",
                "Accept-Encoding": "gzip, deflate",
            }
        )
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        #: Hosts that do not resolve or refuse TLS. Retrying these is pure
        #: latency - discovery probes up to 7 URLs per company, so one dead
        #: domain otherwise costs ~20 pointless requests.
        self._dead_hosts: set[str] = set()
        #: Timeouts seen per host this run. A host that accepts the connection
        #: and then hangs is not "permanent" by the test above, so it used to
        #: be retried on every probe.
        self._timeouts: dict[str, int] = {}

    @staticmethod
    def _is_permanent(exc: Exception) -> bool:
        """DNS failure, refused connection or TLS handshake failure: no retry."""
        if isinstance(exc, SSLError):
            return True
        if isinstance(exc, ReqConnectionError):
            text = str(exc)
            return any(marker in text for marker in (
                "getaddrinfo failed", "NameResolutionError", "Name or service not known",
                "actively refused", "nodename nor servname",
            ))
        return False

    def request(self, method: str, url: str, **kw) -> requests.Response | None:
        """Returns None on unrecoverable failure rather than raising.

        A dead source must never take down the whole scan.
        """
        parsed = urlsplit(url)
        # `hostname` excludes userinfo and query/path credentials; `netloc`
        # could contain `user:password@host`.
        host = parsed.hostname or parsed.netloc
        if host in self._dead_hosts:
            return None

        kw.setdefault("timeout", self.timeout)
        last_exc: Exception | None = None
        answered = False

        for attempt in range(self.retries + 1):
            _limiter.wait(host)
            try:
                resp = self.session.request(method, url, **kw)
            except requests.RequestException as exc:
                last_exc = exc
                if self._is_permanent(exc):
                    log.debug("%s is unreachable, skipping it for this run", host)
                    self._dead_hosts.add(host)
                    return None
                # URLs may contain API keys (for example Jooble's key is part
                # of its path). Never put a URL or exception body in logs.
                log.debug(
                    "%s request to %s failed (%s), attempt %d",
                    method, host, type(exc).__name__, attempt + 1,
                )
            else:
                # The host answered - even a 503 proves it is alive - so record
                # that and clear its strikes here, before the retry path below
                # can skip past this point with `continue`.
                answered = True
                self._timeouts.pop(host, None)
                if resp.status_code in RETRY_STATUS and attempt < self.retries:
                    # Respect Retry-After when the server bothers to send one.
                    wait = float(resp.headers.get("Retry-After") or 0) or 2 ** attempt * 1.5
                    log.debug("%s -> %s, backing off %.1fs", host, resp.status_code, wait)
                    time.sleep(min(wait, 30))
                    continue
                return resp
            if attempt < self.retries:
                time.sleep(2 ** attempt * 1.5)

        # Every attempt timed out and none of them got a response. Such a host
        # is not "permanent" by the test
        # above, and discovery probes up to 7 URLs per domain, so one hanging
        # domain cost ~280s of pure waiting. Only fully exhausted requests
        # count, and only consecutively: one shared hostname can back dozens of
        # configured boards, so transient timeouts must never disable it.
        if isinstance(last_exc, requests.Timeout) and not answered:
            self._timeouts[host] = self._timeouts.get(host, 0) + 1
            if self._timeouts[host] >= TIMEOUT_STRIKES:
                log.debug("%s keeps timing out, skipping it for this run", host)
                self._dead_hosts.add(host)

        failure = type(last_exc).__name__ if last_exc else "unknown error"
        log.warning("giving up on %s (%s)", host, failure)
        return None

    def get_json(self, url: str, **kw):
        resp = self.request("GET", url, **kw)
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            parsed = urlsplit(url)
            log.debug("non-JSON body from %s", parsed.hostname or parsed.netloc)
            return None

    def post_json(self, url: str, payload: dict, **kw):
        resp = self.request("POST", url, json=payload, **kw)
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def get_text(self, url: str, **kw) -> str | None:
        resp = self.request("GET", url, **kw)
        if resp is None or resp.status_code != 200:
            return None
        return resp.text
