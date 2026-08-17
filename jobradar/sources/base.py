"""Source interface. A source yields Jobs and is never allowed to raise."""

from __future__ import annotations

import logging
from typing import Iterator

from ..http import Http
from ..models import Job

log = logging.getLogger("jobradar.sources")


class Source:
    """Base class. `name` is what shows up in the dashboard's source filter."""

    name: str = "unnamed"
    #: True for sources that read a company's own ATS. These win dedup ties
    #: because they carry the real apply link, not an aggregator redirect.
    direct: bool = False

    def fetch(self, http: Http) -> Iterator[Job]:
        raise NotImplementedError

    def safe_fetch(self, http: Http) -> tuple[list[Job], str | None]:
        """-> (jobs, error). One broken source must not abort the scan."""
        try:
            jobs = [j for j in self.fetch(http) if j is not None]
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            log.warning("source %s failed: %s: %s", self.name, type(exc).__name__, exc)
            return [], f"{type(exc).__name__}: {exc}"
        return jobs, None


class CompanySource(Source):
    """An ATS board belonging to one company.

    `token` is whatever that ATS calls the board identifier: a Greenhouse board
    token, a Lever account slug, an Ashby org name, a Workday tenant/site pair.
    """

    direct = True
    platform: str = ""

    def __init__(self, company: str, token: str, **extra):
        self.company = company
        self.token = token
        self.extra = extra
        self.name = f"ats:{self.platform}"

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.company} token={self.token!r}>"
