"""Ensure request failures cannot leak URL-embedded API keys to logs."""

import logging
import sys
from io import StringIO
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar.http import Http  # noqa: E402

secret = "super-secret-api-key"
url = f"https://api.example.test/v1/{secret}/jobs?app_key={secret}"
client = Http(retries=0)


def fail(*_args, **_kwargs):
    raise requests.ConnectionError(f"failed while requesting {url}")


client.session.request = fail
stream = StringIO()
handler = logging.StreamHandler(stream)
logger = logging.getLogger("jobradar.http")
old_level = logger.level
logger.setLevel(logging.DEBUG)
logger.addHandler(handler)
try:
    result = client.get_json(url)
finally:
    logger.removeHandler(handler)
    logger.setLevel(old_level)

logged = stream.getvalue()
failures = []
if result is not None:
    failures.append("failed request did not return None")
if secret in logged:
    failures.append("secret appeared in logs")
if "/v1/" in logged or "app_key" in logged:
    failures.append("URL path or query appeared in logs")
if "api.example.test" not in logged:
    failures.append("safe hostname context was not logged")

# --- hosts that hang, without disabling ones that merely stutter ------------
# _is_permanent only covers DNS, refused connections and TLS, so a host that
# accepts the connection and then hangs was retried on all seven discovery
# probes. Strikes therefore count only fully exhausted requests, and only
# consecutive ones: a single shared hostname backs dozens of configured
# boards, so two unrelated transient timeouts must never disable it.
import jobradar.http as _http  # noqa: E402

_http._limiter.wait = lambda host: None   # keep the test instant
_http.time.sleep = lambda _seconds: None  # ...including retry backoff


def _session(script):
    """A fake session consuming one entry per ATTEMPT; runs out into timeouts.

    An entry is "timeout", "ok", or a status code such as 503.
    """
    seq, calls = list(script), {"n": 0}

    class _S:
        def request(self, _method, url, **_kw):
            calls["n"] += 1
            what = seq.pop(0) if seq else "timeout"
            if what == "timeout":
                raise requests.Timeout("hang")
            resp = requests.Response()
            resp.status_code = 200 if what == "ok" else int(what)
            resp._content = b"ok"
            resp.url = url
            return resp

        def mount(self, *_a, **_kw):
            pass

    return _S(), calls


# A host that only hangs is dropped, and the probes after that cost nothing.
slow = Http(timeout=1, retries=1)
slow.session, slow_calls = _session([])
for _i in range(7):
    slow.request("GET", "https://slow.example.test/careers")
budget = _http.TIMEOUT_STRIKES * 2          # strikes x (1 attempt + 1 retry)
if slow_calls["n"] > budget:
    failures.append(f"hanging host cost {slow_calls['n']} attempts, expected {budget}")
if "slow.example.test" not in slow._dead_hosts:
    failures.append("hanging host was not dropped for the run")

# The regression: timeout then success, twice over. Each request is killed
# before its retry only if strikes survive a success - they must not.
flaky = Http(timeout=1, retries=1)
flaky.session, _ = _session(["timeout", "ok"] * 6)
if not all(flaky.request("GET", "https://shared-ats.example.test/" + p) is not None
           for p in ("a", "b", "c", "d")):
    failures.append("a host that recovers on retry returned None")
if "shared-ats.example.test" in flaky._dead_hosts:
    failures.append("transient timeouts disabled a healthy shared host")
if flaky._timeouts.get("shared-ats.example.test"):
    failures.append("strikes were not cleared by a successful response")

# ...but two exhausted requests in a row, with no success between, do drop it.
gone = Http(timeout=1, retries=1)
gone.session, _ = _session(["timeout"] * 4)
gone.request("GET", "https://gone.example.test/1")
gone.request("GET", "https://gone.example.test/2")
if "gone.example.test" not in gone._dead_hosts:
    failures.append("consecutive exhausted timeouts did not drop the host")

# A retryable status proves the host is alive, so it must clear strikes even
# when the request then fails: the retry path reaches `continue` and used to
# skip the reset entirely. "timeout, timeout" followed by "503, timeout" must
# not disable a host that dozens of configured boards share.
mixed = Http(timeout=1, retries=1)
mixed.session, _ = _session(["timeout", "timeout", "503", "timeout"])
mixed.request("GET", "https://mixed.example.test/1")
mixed.request("GET", "https://mixed.example.test/2")
if "mixed.example.test" in mixed._dead_hosts:
    failures.append("a 503 before a timeout disabled a host that had answered")
if mixed._timeouts.get("mixed.example.test"):
    failures.append("a retryable response did not clear timeout strikes")

if failures:
    print("FAILED:\n  - " + "\n  - ".join(failures))
    sys.exit(1)
print("HTTP logs redact URL paths, queries, and exception text.")
