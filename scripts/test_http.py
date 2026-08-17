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

if failures:
    print("FAILED:\n  - " + "\n  - ".join(failures))
    sys.exit(1)
print("HTTP logs redact URL paths, queries, and exception text.")
