"""Source registry: turns YAML config into live Source objects."""

from __future__ import annotations

import logging

from .ats import ATS_CLASSES
from .base import CompanySource, Source
from .boards import (
    Adzuna, AmazonJobs, Arbeitnow, JobsCh, Jooble, LinkedIn, MyScience,
    SwissDevJobs,
)

log = logging.getLogger("jobradar.sources")

BOARD_CLASSES = {
    "jobs.ch": JobsCh,
    "linkedin": LinkedIn,
    "adzuna": Adzuna,
    "jooble": Jooble,
    "arbeitnow": Arbeitnow,
    "myscience": MyScience,
    "swissdevjobs": SwissDevJobs,
    "amazon": AmazonJobs,
}

__all__ = ["Source", "CompanySource", "ATS_CLASSES", "BOARD_CLASSES", "build_sources"]


def build_company_sources(companies: list[dict]) -> list[CompanySource]:
    """companies.yaml entries -> CompanySource instances."""
    built: list[CompanySource] = []
    for entry in companies or []:
        if not entry.get("enabled", True):
            continue
        platform = entry.get("platform")
        cls = ATS_CLASSES.get(platform)
        if cls is None:
            log.warning("unknown ATS platform %r for %s", platform, entry.get("name"))
            continue
        kwargs = {k: v for k, v in entry.items()
                  if k not in ("name", "platform", "token", "enabled", "tier", "note")}
        try:
            built.append(cls(entry["name"], entry["token"], **kwargs))
        except (KeyError, TypeError) as exc:
            log.warning("bad company entry %s: %s", entry.get("name"), exc)
    return built


def build_board_sources(settings: dict, secrets: dict) -> list[Source]:
    """settings.yaml `boards` block -> board Source instances."""
    queries = settings.get("queries", [])
    locations = settings.get("locations", [])
    built: list[Source] = []

    for name, cfg in (settings.get("boards") or {}).items():
        if not (cfg or {}).get("enabled", True):
            continue
        cls = BOARD_CLASSES.get(name)
        if cls is None:
            log.warning("unknown board source %r", name)
            continue

        opts = {k: v for k, v in (cfg or {}).items() if k != "enabled"}
        # Per-source query/location overrides, else the global sweep.
        q = opts.pop("queries", queries)
        loc = opts.pop("locations", locations)

        if name == "adzuna":
            creds = secrets.get("adzuna", {})
            built.append(cls(q, loc, app_id=creds.get("app_id", ""),
                             app_key=creds.get("app_key", ""), **opts))
        elif name == "jooble":
            built.append(cls(q, loc, api_key=secrets.get("jooble", {}).get("api_key", ""), **opts))
        elif name in ("arbeitnow", "myscience", "swissdevjobs"):
            built.append(cls(**opts))
        else:
            built.append(cls(q, loc, **opts))

    return built


def build_sources(settings: dict, companies: list[dict], secrets: dict) -> list[Source]:
    return build_board_sources(settings, secrets) + build_company_sources(companies)
