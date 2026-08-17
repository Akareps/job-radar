"""Aggregator and job-board adapters.

jobs.ch and LinkedIn are queried through the same public endpoints their own
web front-ends use, logged out, at a deliberately slow rate (see http.py).
Adzuna and Jooble are official APIs and only activate when a key is configured.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator
from urllib.parse import quote_plus, urlencode

from xml.etree import ElementTree

from bs4 import BeautifulSoup

from ..http import Http
from ..models import Job
from ..geo import fold
from ..text import parse_date, strip_html
from .base import Source

log = logging.getLogger("jobradar.sources.boards")


class QuerySource(Source):
    """Base for sources driven by (query x location) sweeps."""

    def __init__(self, queries: list[str], locations: list[str], **opts):
        self.queries = queries
        self.locations = locations
        self.opts = opts


# --------------------------------------------------------------------------- #
# jobs.ch - the largest Swiss board, and it has a real public JSON API that
# returns structured language requirements and experience levels.
# --------------------------------------------------------------------------- #

class JobsCh(QuerySource):
    name = "jobs.ch"
    API = "https://www.jobs.ch/api/v1/public/search"

    # jobs.ch language levels: 1=basic .. 4=native. 3+ means you actually need it.
    FLUENT = 3

    def fetch(self, http: Http) -> Iterator[Job]:
        pages = int(self.opts.get("pages", 2))
        for query in self.queries:
            for location in self.locations:
                for page in range(1, pages + 1):
                    params = {
                        "query": query, "location": location,
                        "rows": 20, "page": page,
                    }
                    data = http.get_json(f"{self.API}?{urlencode(params)}")
                    if not data:
                        break
                    docs = data.get("documents") or []
                    if not docs:
                        break
                    for d in docs:
                        job = self._to_job(d)
                        if job:
                            yield job
                    if page >= (data.get("num_pages") or 1):
                        break

    def _to_job(self, d: dict) -> Job | None:
        job_id = d.get("job_id")
        if not job_id:
            return None
        links = d.get("_links") or {}
        url = ((links.get("detail_en") or links.get("detail_de") or {}).get("href")) or ""

        required = [
            ls.get("language", "")
            for ls in (d.get("language_skills") or [])
            if (ls.get("level") or 0) >= self.FLUENT
        ]
        years = None
        for we in d.get("work_experience") or []:
            # jobs.ch encodes experience bands; take the lower bound.
            if isinstance(we, dict) and we.get("value_min") is not None:
                years = int(we["value_min"])
            elif isinstance(we, (int, float)):
                years = int(we)

        return Job(
            source=self.name,
            source_id=str(job_id),
            title=d.get("title", ""),
            company=d.get("company_name", "") or "",
            url=url,
            apply_url=url,
            location=d.get("place", "") or "",
            country="Switzerland",
            description=strip_html(d.get("preview", "")),
            posted_at=parse_date(d.get("initial_publication_date") or d.get("publication_date")),
            required_languages=required,
            min_years_experience=years,
            raw={"age": d.get("age")},
        )


# --------------------------------------------------------------------------- #
# LinkedIn public guest endpoint (no login, no cookies, no account risk).
# --------------------------------------------------------------------------- #

class LinkedIn(QuerySource):
    name = "linkedin"
    API = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

    #: f_E experience filters: 1 internship, 2 entry, 3 associate, 4 mid-senior,
    #: 5 director, 6 executive. Defaults to junior-facing levels; add 4 if you
    #: want the prop shops that mistag graduate roles as mid-senior.
    EXPERIENCE = "1,2,3"
    #: f_TPR=r604800 -> posted in the last 7 days.
    POSTED_WITHIN = "r604800"

    def fetch(self, http: Http) -> Iterator[Job]:
        pages = int(self.opts.get("pages", 2))
        experience = str(self.opts.get("experience", self.EXPERIENCE))
        posted_within = str(self.opts.get("posted_within", self.POSTED_WITHIN))
        for query in self.queries:
            for location in self.locations:
                for page in range(pages):
                    url = (
                        f"{self.API}?keywords={quote_plus(query)}"
                        f"&location={quote_plus(location)}"
                        f"&f_TPR={posted_within}"
                        f"&f_E={experience}"
                        f"&start={page * 10}"
                    )
                    html = http.get_text(url, headers={"Accept": "text/html"})
                    if not html or "base-card" not in html:
                        break
                    found = 0
                    for job in self._parse(html, location):
                        found += 1
                        yield job
                    if found < 10:
                        break

    def _parse(self, html: str, location_hint: str) -> Iterator[Job]:
        soup = BeautifulSoup(html, "html.parser")
        for card in soup.select("li"):
            title_el = card.select_one(".base-search-card__title")
            company_el = card.select_one(".base-search-card__subtitle")
            loc_el = card.select_one(".job-search-card__location")
            link_el = card.select_one("a.base-card__full-link") or card.select_one("a[href]")
            time_el = card.select_one("time")
            if not (title_el and link_el):
                continue

            href = (link_el.get("href") or "").split("?")[0]
            match = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)", href)
            source_id = match.group(1) if match else href
            if not source_id:
                continue

            yield Job(
                source=self.name,
                source_id=source_id,
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "",
                url=href,
                apply_url=href,
                location=(loc_el.get_text(strip=True) if loc_el else location_hint),
                description="",  # guest cards carry no body; title+company is enough to rank
                posted_at=parse_date(
                    (time_el.get("datetime") if time_el else None)
                    or (time_el.get_text(strip=True) if time_el else None)
                ),
                raw={},
            )


# --------------------------------------------------------------------------- #
# Adzuna - official API, free key, Switzerland + UK + NL all supported.
# --------------------------------------------------------------------------- #

class Adzuna(QuerySource):
    name = "adzuna"
    API = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

    def __init__(self, queries, locations, app_id: str = "", app_key: str = "", **opts):
        super().__init__(queries, locations, **opts)
        self.app_id = app_id
        self.app_key = app_key
        self.countries = opts.get("countries", ["ch", "gb", "nl"])

    def fetch(self, http: Http) -> Iterator[Job]:
        if not (self.app_id and self.app_key):
            log.info("adzuna: no API key configured, skipping")
            return
        for country in self.countries:
            for query in self.queries:
                params = {
                    "app_id": self.app_id, "app_key": self.app_key,
                    "results_per_page": 50, "what": query,
                    "max_days_old": int(self.opts.get("max_days_old", 14)),
                    "content-type": "application/json",
                }
                where = self.opts.get("where", {}).get(country)
                if where:
                    params["where"] = where
                data = http.get_json(
                    f"{self.API.format(country=country, page=1)}?{urlencode(params)}"
                )
                if not data:
                    continue
                for r in data.get("results", []):
                    yield Job(
                        source=self.name,
                        source_id=str(r.get("id")),
                        title=r.get("title", ""),
                        company=(r.get("company") or {}).get("display_name", ""),
                        url=r.get("redirect_url", ""),
                        apply_url=r.get("redirect_url", ""),
                        location=(r.get("location") or {}).get("display_name", ""),
                        country=country.upper(),
                        description=strip_html(r.get("description", "")),
                        posted_at=parse_date(r.get("created")),
                        employment_type=r.get("contract_time", "") or "",
                        department=(r.get("category") or {}).get("label", ""),
                        raw={},
                    )


# --------------------------------------------------------------------------- #
# Jooble - official API, free key on request, good DACH coverage.
# --------------------------------------------------------------------------- #

class Jooble(QuerySource):
    name = "jooble"
    API = "https://jooble.org/api/{key}"

    def __init__(self, queries, locations, api_key: str = "", **opts):
        super().__init__(queries, locations, **opts)
        self.api_key = api_key

    def fetch(self, http: Http) -> Iterator[Job]:
        if not self.api_key:
            log.info("jooble: no API key configured, skipping")
            return
        url = self.API.format(key=self.api_key)
        for query in self.queries:
            for location in self.locations:
                data = http.post_json(url, {"keywords": query, "location": location, "page": "1"})
                if not data:
                    continue
                for j in data.get("jobs", []):
                    yield Job(
                        source=self.name,
                        source_id=str(j.get("id")),
                        title=j.get("title", ""),
                        company=j.get("company", "") or "",
                        url=j.get("link", ""),
                        apply_url=j.get("link", ""),
                        location=j.get("location", "") or location,
                        description=strip_html(j.get("snippet", "")),
                        posted_at=parse_date(j.get("updated")),
                        employment_type=j.get("type", "") or "",
                        raw={},
                    )


# --------------------------------------------------------------------------- #
# Arbeitnow - free, no key, strong DACH coverage.
# --------------------------------------------------------------------------- #

class Arbeitnow(Source):
    name = "arbeitnow"
    API = "https://www.arbeitnow.com/api/job-board-api"

    def __init__(self, pages: int = 3, **_):
        self.pages = pages

    def fetch(self, http: Http) -> Iterator[Job]:
        for page in range(1, self.pages + 1):
            data = http.get_json(f"{self.API}?page={page}")
            if not data:
                return
            items = data.get("data") or []
            if not items:
                return
            for j in items:
                yield Job(
                    source=self.name,
                    source_id=str(j.get("slug")),
                    title=j.get("title", ""),
                    company=j.get("company_name", "") or "",
                    url=j.get("url", ""),
                    apply_url=j.get("url", ""),
                    location=j.get("location", "") or "",
                    description=strip_html(j.get("description", "")),
                    posted_at=parse_date(j.get("created_at")),
                    employment_type=", ".join(j.get("job_types") or []),
                    remote=bool(j.get("remote")),
                    raw={},
                )


# --------------------------------------------------------------------------- #
# SwissDevJobs - Swiss developer/data board. Its documented JSON API now
# answers "ENDPOINT Deprecated", but the RSS feed is live and complete.
# --------------------------------------------------------------------------- #

class SwissDevJobs(Source):
    name = "swissdevjobs"
    RSS = "https://swissdevjobs.ch/rss"

    #: "Data Engineer @ Acme AG [CHF 100'000 - 130'000]"
    _TITLE = re.compile(r"^(?P<title>.+?)\s+@\s+(?P<company>.+?)(?:\s*\[(?P<salary>[^\]]*)\])?\s*$")

    #: The feed carries no location field, so the office has to be recovered
    #: from the body. This list is local to the adapter on purpose - it is a
    #: Switzerland-only board, and the generic location model has no business
    #: knowing about Swiss municipalities.
    _CITIES = (
        "zurich", "zuerich", "geneva", "genf", "basel", "bern", "lausanne",
        "winterthur", "lucerne", "luzern", "st gallen", "lugano", "biel",
        "thun", "koniz", "la chaux de fonds", "schaffhausen", "fribourg",
        "chur", "neuchatel", "vernier", "uster", "sion", "emmen", "zug",
        "yverdon", "kriens", "rapperswil", "dietikon", "montreux", "baar",
        "schlieren", "wallisellen", "opfikon", "duebendorf", "dubendorf",
        "allschwil", "pratteln", "muttenz", "rotkreuz", "cham", "baden",
        "aarau", "olten", "solothurn", "frauenfeld", "wil", "kloten",
    )

    def _city_in(self, text: str) -> str:
        blob = f" {fold(text[:4000])} "
        for city in self._CITIES:
            if f" {city} " in blob:
                return city.title()
        return ""

    def fetch(self, http: Http) -> Iterator[Job]:
        xml = http.get_text(self.RSS)
        if not xml or "<item>" not in xml:
            return
        try:
            root = ElementTree.fromstring(xml.encode("utf-8"))
        except ElementTree.ParseError:
            log.warning("swissdevjobs: RSS did not parse")
            return

        for item in root.iter("item"):
            def field(tag: str) -> str:
                node = item.find(tag)
                return (node.text or "").strip() if node is not None and node.text else ""

            raw_title = field("title")
            link = field("link")
            if not raw_title or not link:
                continue

            match = self._TITLE.match(raw_title)
            if match:
                title = match.group("title").strip()
                company = match.group("company").strip()
                salary = (match.group("salary") or "").strip()
            else:
                title, company, salary = raw_title, "", ""

            body = strip_html(field("description"))
            # The feed carries no location field, so recover the office from
            # the body; everything here is Switzerland regardless.
            city = self._city_in(f"{body} {title}")

            yield Job(
                source=self.name,
                source_id=field("guid") or link.split("?")[0],
                title=title,
                company=company,
                url=link,
                apply_url=link,
                location=city or "Switzerland",
                country="Switzerland",
                description=(f"Salary: {salary}\n\n" if salary else "") + body,
                posted_at=parse_date(field("pubDate")),
                raw={"salary": salary},
            )


# --------------------------------------------------------------------------- #
# Amazon - public search.json behind amazon.jobs. Amazon's in-house careers
# system is not reached by the ATS adapters.
# --------------------------------------------------------------------------- #

class AmazonJobs(QuerySource):
    name = "amazon"
    API = "https://www.amazon.jobs/en/search.json"
    BASE = "https://www.amazon.jobs"

    #: `loc_query` alone is advisory and gets ignored - asking for Switzerland
    #: returns Bengaluru and Seattle. The ISO-3 `country` filter is the one
    #: that actually constrains results.
    COUNTRY = {
        "switzerland": "CHE", "united kingdom": "GBR", "uk": "GBR",
        "netherlands": "NLD", "germany": "DEU", "france": "FRA",
    }

    def _country_for(self, location: str) -> str | None:
        tail = location.split(",")[-1].strip().lower()
        return self.COUNTRY.get(tail)

    def fetch(self, http: Http) -> Iterator[Job]:
        # The country filter is country-wide, so Zurich and Basel would each
        # return the identical Swiss result set. Query one location per country.
        by_country: dict[str, str] = {}
        for location in self.locations:
            country = self._country_for(location)
            if not country:
                log.info("amazon: no country code for %r, skipping", location)
                continue
            by_country.setdefault(country, location)

        for country, location in by_country.items():
            for offset in (0, 100):
                params = {
                    "base_query": "", "loc_query": location, "country": country,
                    "result_limit": 100, "offset": offset, "sort": "recent",
                }
                data = http.get_json(f"{self.API}?{urlencode(params)}")
                if not data:
                    break
                jobs = data.get("jobs") or []
                if not jobs:
                    break
                for j in jobs:
                    path = j.get("job_path") or ""
                    yield Job(
                        source=self.name,
                        source_id=str(j.get("id_icims") or j.get("id") or path),
                        title=j.get("title", ""),
                        company=j.get("company_name") or "Amazon",
                        url=self.BASE + path,
                        apply_url=self.BASE + path,
                        location=j.get("normalized_location")
                                 or ", ".join(x for x in (j.get("city"), j.get("state")) if x),
                        country=j.get("country_code", "") or "",
                        description=strip_html(
                            (j.get("description_short") or "") + " "
                            + (j.get("basic_qualifications") or "")
                        ),
                        posted_at=parse_date(j.get("posted_date")),
                        department=j.get("job_category", "") or "",
                        raw={},
                    )
                if len(jobs) < 100:
                    break


# --------------------------------------------------------------------------- #
# myScience.ch - Swiss academic and research posts (ETH, UZH, EPFL, PSI, Empa).
# Academic and research vacancies from Swiss and European institutions.
# --------------------------------------------------------------------------- #

class MyScience(Source):
    """Swiss academic and research posts (ETH, UZH, EPFL, PSI, Empa, hospitals).

    Each listing row renders as pipe-separated text:
        category | place | - | 5 Aug | Job title | Employer [| group]
    We read the category pages that overlap your background rather than the
    whole board, which is mostly professorships and lab-bench roles.
    """

    name = "myscience"
    BASE = "https://www.myscience.ch"
    CATEGORIES = [
        "Computer+Science",
        "Life+Sciences",
        "Mathematics",
        "Innovation",
        "Physics",
        "Interdisciplinary-All+Categories",
    ]
    _ID = re.compile(r"/jobs/id(\d+)-")
    _DATE = re.compile(r"^\d{1,2}\s+\w{3,}$")

    def __init__(self, pages: int = 3, categories: list[str] | None = None, **_):
        self.pages = pages
        self.categories = categories or self.CATEGORIES

    def fetch(self, http: Http) -> Iterator[Job]:
        seen: set[str] = set()
        for category in self.categories[: max(1, self.pages) + 3]:
            html = http.get_text(f"{self.BASE}/en/jobs/{category}")
            if not html:
                continue
            for job in self._parse(html, seen):
                yield job

    def _parse(self, html: str, seen: set[str]) -> Iterator[Job]:
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.select("a[href]"):
            href = link.get("href") or ""
            match = self._ID.search(href)
            if not match or match.group(1) in seen:
                continue

            block = link.find_parent(["li", "article", "div", "tr"])
            if block is None:
                continue
            parts = [p.strip() for p in block.get_text(" | ", strip=True).split("|")
                     if p.strip() and p.strip() != "-"]
            if len(parts) < 4:
                continue

            seen.add(match.group(1))
            category, place, posted, title, employer = self._fields(parts)
            if not title:
                continue

            yield Job(
                source=self.name,
                source_id=match.group(1),
                title=title,
                company=employer,
                url=href if href.startswith("http") else self.BASE + href,
                apply_url=href if href.startswith("http") else self.BASE + href,
                location=place,
                # myScience carries international posts too (Max Planck, CERN),
                # so let the location classifier decide the country from `place`
                # rather than assuming Switzerland.
                country="",
                description=f"{category}. {title} at {employer}.",
                posted_at=parse_date(posted, fallback_now=True),
                department=category,
                raw={},
            )

    def _fields(self, parts: list[str]) -> tuple[str, str, str, str, str]:
        """parts looks like [category, place, '5 Aug', title, employer, ...]."""
        category = parts[0] if parts else ""
        date_idx = next(
            (i for i, p in enumerate(parts) if self._DATE.match(p)), None
        )
        if date_idx is None:
            # No date column: assume [category, place, title, employer].
            place = parts[1] if len(parts) > 1 else ""
            title = parts[2] if len(parts) > 2 else ""
            employer = parts[3] if len(parts) > 3 else ""
            return category, place, "", title, employer

        place = " ".join(parts[1:date_idx])[:80]
        posted = parts[date_idx]
        title = parts[date_idx + 1] if len(parts) > date_idx + 1 else ""
        employer = parts[date_idx + 2] if len(parts) > date_idx + 2 else ""
        return category, place, posted, title.strip(), employer[:120]
