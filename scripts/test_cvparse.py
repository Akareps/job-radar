"""The CV extractor must never echo transport error text.

A proxy URL can carry user:password, and the SDK does not wrap transport
failures, so the catch-all sees raw exception text.

Run: python scripts/test_cvparse.py
"""

import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

# anthropic is an optional extra; stub it so this runs on a base install.
_fake = types.ModuleType("anthropic")


class _Err(Exception):
    pass


class _Status(_Err):
    def __init__(self, *_a, **_k):
        self.status_code, self.message = 500, "x"


_fake.AuthenticationError = type("AuthenticationError", (_Err,), {})
_fake.RateLimitError = type("RateLimitError", (_Err,), {})
_fake.APIStatusError = _Status
_fake.APIConnectionError = type("APIConnectionError", (_Err,), {})
sys.modules.setdefault("anthropic", _fake)

from jobradar import cvparse  # noqa: E402

failures = []
SECRET = "socks5://user:hunter2SUPERSECRET@proxy.internal:1080"

cv_path = Path(tempfile.mkdtemp()) / "cv.txt"
cv_path.write_text(
    """Maya Example
Berlin, Germany

EXPERIENCE
Senior Backend Engineer, Example GmbH, 2021-2025. Built and operated payment
services in Python and Go on Kubernetes. Led a migration from a monolith to
event-driven services, and mentored three engineers.

Platform Engineer, Another Company, 2019-2021. Terraform, AWS, CI/CD pipelines
and observability with Prometheus and Grafana.

EDUCATION
MSc Computer Science, TU Berlin, 2019.

SKILLS
Python, Go, PostgreSQL, Kubernetes, Terraform, AWS, gRPC, Kafka.
""",
    encoding="utf-8",
)


def _expect_clean(label, build):
    _fake.Anthropic = build
    try:
        cvparse.from_cv(cv_path)
    except RuntimeError as exc:
        if "hunter2SUPERSECRET" in str(exc):
            failures.append(f"{label}: proxy credentials appeared in the message")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"{label}: raised {type(exc).__name__} instead of RuntimeError")
    else:
        failures.append(f"{label}: did not raise")


def _transport_boom(*_a, **_k):
    raise ImportError(f"Using SOCKS proxy, but socksio is not installed. proxy={SECRET}")


def _connection_boom(*_a, **_k):
    def _create(**_kw):
        raise _fake.APIConnectionError(f"failed to connect via {SECRET}")
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=_create))


_expect_clean("transport error", _transport_boom)
_expect_clean("connection error", _connection_boom)

# With a SOCKS proxy and no socksio, the client must never be constructed:
# httpx raises while building the transport and again from __del__.
built = {"n": 0}


def _counting(*_a, **_k):
    built["n"] += 1
    raise ImportError("should not be reached")


os.environ["ALL_PROXY"] = SECRET
try:
    _expect_clean("socks preflight", _counting)
finally:
    os.environ.pop("ALL_PROXY", None)
if built["n"]:
    failures.append("client was constructed despite unusable SOCKS settings")

if failures:
    print("FAILED:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("CV extraction fails cleanly without echoing transport text.")
