"""Company-direct ATS adapters.

These hit the same public JSON endpoints a company's own careers page calls to
render itself. No auth, no keys, stable for years, and they surface postings
before aggregators pick them up. This is the core of the whole app.

Every adapter's `discover()` classmethod is used by scripts/discover_ats.py to
work out which platform a company is on.
"""

from __future__ import annotations

import re
from typing import Iterator
from xml.etree import ElementTree

from ..http import Http
from ..models import Job
from ..text import parse_date, strip_html
from .base import CompanySource


class Greenhouse(CompanySource):
    """boards-api.greenhouse.io - used by most US-style tech companies."""

    platform = "greenhouse"
    API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"

    def fetch(self, http: Http) -> Iterator[Job]:
        data = http.get_json(self.API.format(token=self.token))
        if not data:
            return
        for j in data.get("jobs", []):
            loc = (j.get("location") or {}).get("name", "")
            offices = ", ".join(
                o.get("name", "") for o in (j.get("offices") or []) if o.get("name")
            )
            yield Job(
                source=self.name,
                source_id=f"{self.token}:{j.get('id')}",
                title=j.get("title", ""),
                company=j.get("company_name") or self.company,
                url=j.get("absolute_url", ""),
                apply_url=j.get("absolute_url", ""),
                location=loc or offices,
                description=strip_html(j.get("content", "")),
                posted_at=parse_date(j.get("first_published") or j.get("updated_at")),
                department=", ".join(
                    d.get("name", "") for d in (j.get("departments") or [])
                ),
                raw=j,
            )

    @classmethod
    def discover(cls, http: Http, token: str) -> int | None:
        data = http.get_json(cls.API.format(token=token))
        return len(data.get("jobs", [])) if isinstance(data, dict) and "jobs" in data else None

    @classmethod
    def board_identity(cls, http: Http, token: str) -> str | None:
        """The employer name the board reports for itself.

        Used to reject name-guessed tokens that resolve to a different company.
        """
        data = http.get_json(cls.API.format(token=token))
        jobs = (data or {}).get("jobs") or []
        return jobs[0].get("company_name") if jobs else None


class Lever(CompanySource):
    """api.lever.co - common with European scale-ups."""

    platform = "lever"
    API = "https://api.lever.co/v0/postings/{token}?mode=json"

    def fetch(self, http: Http) -> Iterator[Job]:
        data = http.get_json(self.API.format(token=self.token))
        if not isinstance(data, list):
            return
        for j in data:
            cats = j.get("categories") or {}
            locations = cats.get("allLocations") or []
            loc = ", ".join(locations) if locations else (cats.get("location") or "")
            body = j.get("descriptionPlain") or strip_html(j.get("description", ""))
            extra = j.get("additionalPlain") or strip_html(j.get("additional", ""))
            yield Job(
                source=self.name,
                source_id=f"{self.token}:{j.get('id')}",
                title=j.get("text", ""),
                company=self.company,
                url=j.get("hostedUrl", ""),
                apply_url=j.get("applyUrl") or j.get("hostedUrl", ""),
                location=loc,
                country=j.get("country") or "",
                description=f"{body}\n\n{extra}".strip(),
                posted_at=parse_date(j.get("createdAt")),
                employment_type=cats.get("commitment", "") or "",
                department=cats.get("department", "") or "",
                remote=(j.get("workplaceType") or "").lower() == "remote",
                raw=j,
            )

    @classmethod
    def discover(cls, http: Http, token: str) -> int | None:
        data = http.get_json(cls.API.format(token=token))
        return len(data) if isinstance(data, list) else None


class Ashby(CompanySource):
    """api.ashbyhq.com - AI labs and newer startups. Org slug is case-sensitive."""

    platform = "ashby"
    API = "https://api.ashbyhq.com/posting-api/job-board/{token}"

    def fetch(self, http: Http) -> Iterator[Job]:
        data = http.get_json(self.API.format(token=self.token))
        if not data:
            return
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            locs = [j.get("location") or ""] + [
                s.get("location", "") if isinstance(s, dict) else str(s)
                for s in (j.get("secondaryLocations") or [])
            ]
            yield Job(
                source=self.name,
                source_id=f"{self.token}:{j.get('id')}",
                title=j.get("title", ""),
                company=self.company,
                url=j.get("jobUrl", ""),
                apply_url=j.get("applyUrl") or j.get("jobUrl", ""),
                location=", ".join(x for x in locs if x),
                description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
                posted_at=parse_date(j.get("publishedAt")),
                employment_type=j.get("employmentType", "") or "",
                department=j.get("department", "") or j.get("team", "") or "",
                remote=bool(j.get("isRemote")),
                raw=j,
            )

    @classmethod
    def discover(cls, http: Http, token: str) -> int | None:
        data = http.get_json(cls.API.format(token=token))
        return len(data.get("jobs", [])) if isinstance(data, dict) and "jobs" in data else None


class SmartRecruiters(CompanySource):
    """api.smartrecruiters.com - very common with Swiss and German employers."""

    platform = "smartrecruiters"
    LIST = "https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset={offset}"
    DETAIL = "https://api.smartrecruiters.com/v1/companies/{token}/postings/{pid}"

    def fetch(self, http: Http) -> Iterator[Job]:
        offset, seen = 0, 0
        while True:
            data = http.get_json(self.LIST.format(token=self.token, offset=offset))
            if not data:
                return
            postings = data.get("content", [])
            if not postings:
                return
            for p in postings:
                loc = p.get("location") or {}
                loc_str = ", ".join(
                    str(x) for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x
                )
                # The list endpoint omits the body; fetch it for the first 40
                # postings only, so a 2000-posting board stays fast.
                description = ""
                if seen < 40:
                    detail = http.get_json(
                        self.DETAIL.format(token=self.token, pid=p.get("id"))
                    )
                    if detail:
                        description = _smartrecruiters_body(detail)
                seen += 1
                yield Job(
                    source=self.name,
                    source_id=f"{self.token}:{p.get('id')}",
                    title=p.get("name", ""),
                    company=(p.get("company") or {}).get("name") or self.company,
                    url=p.get("applyUrl") or p.get("ref", ""),
                    apply_url=p.get("applyUrl", ""),
                    location=loc_str,
                    country=loc.get("country", "") or "",
                    description=description,
                    posted_at=parse_date(p.get("releasedDate") or p.get("createdOn")),
                    employment_type=(p.get("typeOfEmployment") or {}).get("label", ""),
                    department=(p.get("department") or {}).get("label", ""),
                    remote=bool(loc.get("remote")),
                    raw=p,
                )
            offset += len(postings)
            if offset >= data.get("totalFound", 0) or offset > 600:
                return

    @classmethod
    def discover(cls, http: Http, token: str) -> int | None:
        data = http.get_json(cls.LIST.format(token=token, offset=0))
        if isinstance(data, dict) and "totalFound" in data:
            return int(data["totalFound"])
        return None

    @classmethod
    def board_identity(cls, http: Http, token: str) -> str | None:
        data = http.get_json(cls.LIST.format(token=token, offset=0))
        postings = (data or {}).get("content") or []
        return ((postings[0].get("company") or {}).get("name")) if postings else None


def _smartrecruiters_body(detail: dict) -> str:
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    chunks = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        text = (sections.get(key) or {}).get("text")
        if text:
            chunks.append(strip_html(text))
    return "\n\n".join(chunks)


class Workday(CompanySource):
    """myworkdayjobs.com CXS API - UBS, Roche, Novartis, most large employers.

    `token` is "tenant/site" and `host` is the wdN subdomain, e.g.
    token="roche/roche-ext", host="roche.wd3". Workday only accepts limit=20.
    """

    platform = "workday"
    PAGE = 20

    def __init__(self, company: str, token: str, host: str = "", max_pages: int = 12,
                 search: list[str] | None = None, **extra):
        super().__init__(company, token, **extra)
        self.host = host or extra.get("host", "")
        self.max_pages = max_pages
        #: Optional server-side search terms. Workday has no location facet we
        #: can address without tenant-specific ids, and boards like NVIDIA's
        #: (2000 postings) or Accenture's bury their Swiss roles far outside the
        #: newest-N window we would otherwise read. Setting `search: [Zurich,
        #: Basel]` in companies.yaml queries those terms instead.
        self.search = search or []

    @property
    def _endpoint(self) -> str:
        tenant, _, site = self.token.partition("/")
        return f"https://{self.host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"

    @property
    def _base_url(self) -> str:
        tenant, _, site = self.token.partition("/")
        return f"https://{self.host}.myworkdayjobs.com/en-US/{site}"

    def fetch(self, http: Http) -> Iterator[Job]:
        seen: set[str] = set()
        for term in (self.search or [""]):
            yield from self._fetch_term(http, term, seen)

    def _fetch_term(self, http: Http, term: str, seen: set[str]) -> Iterator[Job]:
        # Workday has no server-side date sort, so we walk pages until we have
        # enough. Boards are ordered newest-first in practice.
        for page in range(self.max_pages):
            data = http.post_json(
                self._endpoint,
                {"appliedFacets": {}, "limit": self.PAGE,
                 "offset": page * self.PAGE, "searchText": term},
            )
            if not data:
                return
            postings = data.get("jobPostings", [])
            if not postings:
                return
            for p in postings:
                if (p.get("externalPath") or p.get("title")) in seen:
                    continue
                seen.add(p.get("externalPath") or p.get("title"))
                path = p.get("externalPath", "")
                yield Job(
                    source=self.name,
                    source_id=f"{self.token}:{path or p.get('title')}",
                    title=p.get("title", ""),
                    company=self.company,
                    url=self._base_url + path,
                    apply_url=self._base_url + path,
                    location=_workday_location(p.get("locationsText", ""), path),
                    description=" ".join(p.get("bulletFields") or []),
                    posted_at=parse_date(p.get("postedOn"), fallback_now=False),
                    employment_type=p.get("timeType", "") or "",
                    raw=p,
                )
            if (page + 1) * self.PAGE >= data.get("total", 0):
                return

    @classmethod
    def discover(cls, http: Http, token: str, host: str = "") -> int | None:
        tenant, _, site = token.partition("/")
        url = f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        data = http.post_json(url, {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""})
        return int(data.get("total", 0)) if isinstance(data, dict) and "total" in data else None


#: Workday collapses multi-site postings to "2 Locations", which tells us
#: nothing. The externalPath always carries the primary office:
#: /job/Basel-City/QC-Specialist_REQ-123 -> "Basel City".
_WD_VAGUE = re.compile(r"^\s*\d*\s*locations?\s*$", re.I)


def _workday_location(locations_text: str, path: str) -> str:
    parts = []
    if locations_text and not _WD_VAGUE.match(locations_text):
        parts.append(locations_text)
    segments = [s for s in (path or "").split("/") if s]
    if len(segments) >= 2 and segments[0] == "job":
        parts.append(segments[1].replace("-", " ").replace("_", " ").strip())
    return ", ".join(dict.fromkeys(p for p in parts if p))


class Personio(CompanySource):
    """{tenant}.jobs.personio.de/xml - the default ATS for Swiss/German SMEs.

    Unknown tenants return Personio's marketing page instead of a 404, so a
    board only counts as real when the XML actually parses with positions.
    """

    platform = "personio"
    API = "https://{token}.jobs.personio.de/xml"

    def fetch(self, http: Http) -> Iterator[Job]:
        text = http.get_text(self.API.format(token=self.token))
        if not text or "<position>" not in text:
            return
        try:
            root = ElementTree.fromstring(text.encode("utf-8"))
        except ElementTree.ParseError:
            return

        for pos in root.iter("position"):
            def field(tag: str) -> str:
                node = pos.find(tag)
                return (node.text or "").strip() if node is not None and node.text else ""

            body = " ".join(
                (node.text or "") for node in pos.iter("value") if node.text
            )
            pid = field("id")
            yield Job(
                source=self.name,
                source_id=f"{self.token}:{pid}",
                title=field("name"),
                company=field("subcompany") or self.company,
                url=f"https://{self.token}.jobs.personio.de/job/{pid}",
                apply_url=f"https://{self.token}.jobs.personio.de/job/{pid}",
                location=field("office"),
                description=strip_html(body),
                posted_at=parse_date(field("createdAt")),
                employment_type=field("employmentType"),
                department=field("department"),
                min_years_experience=_personio_years(field("yearsOfExperience")),
                raw={"seniority": field("seniority")},
            )

    @classmethod
    def discover(cls, http: Http, token: str) -> int | None:
        text = http.get_text(cls.API.format(token=token))
        if not text or "<position>" not in text:
            return None
        return text.count("<position>")

    @classmethod
    def board_identity(cls, http: Http, token: str) -> str | None:
        text = http.get_text(cls.API.format(token=token))
        if not text:
            return None
        match = re.search(r"<subcompany>(.*?)</subcompany>", text, re.S)
        return match.group(1).strip() if match else None


def _personio_years(value: str) -> int | None:
    """Personio uses buckets like 'lt-1', '1-2', '2-5', '5-10'."""
    if not value:
        return None
    nums = re.findall(r"\d+", value)
    if value.startswith("lt"):
        return 0
    return int(nums[0]) if nums else None


class Workable(CompanySource):
    """apply.workable.com widget API."""

    platform = "workable"
    API = "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"

    def fetch(self, http: Http) -> Iterator[Job]:
        data = http.get_json(self.API.format(token=self.token))
        if not data:
            return
        for j in data.get("jobs", []):
            loc = ", ".join(
                x for x in (j.get("city"), j.get("state"), j.get("country")) if x
            ) or j.get("location", "")
            yield Job(
                source=self.name,
                source_id=f"{self.token}:{j.get('shortcode') or j.get('id')}",
                title=j.get("title", ""),
                company=data.get("name") or self.company,
                url=j.get("url") or j.get("application_url", ""),
                apply_url=j.get("application_url") or j.get("url", ""),
                location=loc,
                country=j.get("country", "") or "",
                description=strip_html(
                    (j.get("description") or "") + " " + (j.get("requirements") or "")
                ),
                posted_at=parse_date(j.get("published_on") or j.get("created_at")),
                employment_type=j.get("employment_type", "") or "",
                department=j.get("department", "") or "",
                remote=bool(j.get("telecommuting")),
                raw=j,
            )

    @classmethod
    def discover(cls, http: Http, token: str) -> int | None:
        data = http.get_json(cls.API.format(token=token))
        return len(data.get("jobs", [])) if isinstance(data, dict) and "jobs" in data else None

    @classmethod
    def board_identity(cls, http: Http, token: str) -> str | None:
        data = http.get_json(cls.API.format(token=token))
        return (data or {}).get("name")


class Recruitee(CompanySource):
    platform = "recruitee"
    API = "https://{token}.recruitee.com/api/offers/"

    def fetch(self, http: Http) -> Iterator[Job]:
        data = http.get_json(self.API.format(token=self.token))
        if not data:
            return
        for j in data.get("offers", []):
            loc = ", ".join(
                x for x in (j.get("city"), j.get("state_name"), j.get("country")) if x
            ) or j.get("location", "")
            yield Job(
                source=self.name,
                source_id=f"{self.token}:{j.get('id')}",
                title=j.get("title", ""),
                company=self.company,
                url=j.get("careers_url") or j.get("careers_apply_url", ""),
                apply_url=j.get("careers_apply_url", ""),
                location=loc,
                country=j.get("country", "") or "",
                description=strip_html(
                    (j.get("description") or "") + " " + (j.get("requirements") or "")
                ),
                posted_at=parse_date(j.get("published_at") or j.get("created_at")),
                employment_type=j.get("employment_type_code", "") or "",
                department=j.get("department", "") or "",
                remote=bool(j.get("remote")),
                raw=j,
            )

    @classmethod
    def discover(cls, http: Http, token: str) -> int | None:
        data = http.get_json(cls.API.format(token=token))
        return len(data.get("offers", [])) if isinstance(data, dict) and "offers" in data else None

    @classmethod
    def board_identity(cls, http: Http, token: str) -> str | None:
        data = http.get_json(cls.API.format(token=token))
        offers = (data or {}).get("offers") or []
        return offers[0].get("company_name") if offers else None


class Teamtailor(CompanySource):
    """Public careers feed. Teamtailor's JSON:API needs a key, but every board
    exposes an unauthenticated RSS feed which carries enough to rank a job."""

    platform = "teamtailor"
    API = "https://{token}.teamtailor.com/jobs.rss"

    def fetch(self, http: Http) -> Iterator[Job]:
        text = http.get_text(self.API.format(token=self.token))
        if not text or "<item>" not in text:
            return
        try:
            root = ElementTree.fromstring(text.encode("utf-8"))
        except ElementTree.ParseError:
            return
        for item in root.iter("item"):
            def field(tag: str) -> str:
                node = item.find(tag)
                return (node.text or "").strip() if node is not None and node.text else ""

            link = field("link")
            yield Job(
                source=self.name,
                source_id=f"{self.token}:{field('guid') or link}",
                title=field("title"),
                company=self.company,
                url=link,
                apply_url=link,
                location=field("location"),
                description=strip_html(field("description")),
                posted_at=parse_date(field("pubDate")),
                raw={},
            )

    @classmethod
    def discover(cls, http: Http, token: str) -> int | None:
        text = http.get_text(cls.API.format(token=token))
        if not text or "<item>" not in text:
            return None
        return text.count("<item>")


class JoinCom(CompanySource):
    """join.com - popular with small DACH startups."""

    platform = "join"
    API = "https://join.com/api/public/companies/{token}/jobs"

    def fetch(self, http: Http) -> Iterator[Job]:
        data = http.get_json(self.API.format(token=self.token))
        if not data:
            return
        items = data if isinstance(data, list) else data.get("jobs", data.get("data", []))
        for j in items or []:
            yield Job(
                source=self.name,
                source_id=f"{self.token}:{j.get('id') or j.get('slug')}",
                title=j.get("title", ""),
                company=self.company,
                url=j.get("url") or j.get("applyUrl", ""),
                apply_url=j.get("applyUrl") or j.get("url", ""),
                location=j.get("location") or (j.get("city") or ""),
                description=strip_html(j.get("description", "")),
                posted_at=parse_date(j.get("publishedAt") or j.get("createdAt")),
                employment_type=j.get("employmentType", "") or "",
                raw=j,
            )

    @classmethod
    def discover(cls, http: Http, token: str) -> int | None:
        data = http.get_json(cls.API.format(token=token))
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            items = data.get("jobs") or data.get("data")
            return len(items) if isinstance(items, list) else None
        return None


#: platform name -> adapter class. Used by the config loader and the discoverer.
ATS_CLASSES: dict[str, type[CompanySource]] = {
    cls.platform: cls
    for cls in (
        Greenhouse, Lever, Ashby, SmartRecruiters, Workday,
        Personio, Workable, Recruitee, Teamtailor, JoinCom,
    )
}
