"""Canonical job record shared by every source adapter."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


def _slug(s: str) -> str:
    """Lowercase alphanumeric skeleton used for identity and dedup keys."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# Legal-form suffixes, industry filler and board-page cruft that can make the
# same employer look like different companies across sources.
_COMPANY_NOISE = re.compile(
    r"\b(ag|sa|sarl|gmbh|ltd|limited|llc|lp|inc|incorporated|plc|bv|nv|"
    r"holding|holdings|group|schweiz|switzerland|suisse|svizzera|"
    r"international|global|europe|technologies|technology|solutions|"
    r"trading|capital|markets|securities|partners|management|investments|"
    r"advisors|careers|company)\b",
    re.I,
)


def normalize_company(name: str) -> str:
    n = (name or "").lower()
    n = n.replace("&", " and ")
    # Drop periods without inserting a space, so "B.V." collapses to "bv" and
    # the noise list can match it. Replacing them with spaces left "b v".
    n = n.replace(".", "")
    n = re.sub(r"[,'\"()\-/]", " ", n)
    n = _COMPANY_NOISE.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


# Words that vary between postings of the same role across boards.
_TITLE_NOISE = re.compile(
    r"\b(m\s*[/|]\s*f\s*[/|]\s*d|m\s*[/|]\s*w\s*[/|]\s*d|f\s*[/|]\s*m|all genders|"
    r"a\s*\)|100%|80%|60%|full[- ]time|part[- ]time|permanent|fixed[- ]term|"
    r"remote|hybrid|onsite|on[- ]site)\b",
    re.I,
)


def normalize_title(title: str) -> str:
    t = (title or "").lower()
    t = _TITLE_NOISE.sub(" ", t)
    t = re.sub(r"[^a-z0-9+#\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class Job:
    """One posting, normalized. `source_id` is whatever the source calls its key."""

    source: str
    source_id: str
    title: str
    company: str
    url: str

    location: str = ""
    city: str = ""
    country: str = ""
    apply_url: str = ""
    description: str = ""
    posted_at: str | None = None          # ISO 8601 UTC
    employment_type: str = ""
    language: str = "en"                  # detected posting language
    remote: bool = False
    department: str = ""

    # Enrichment filled in by the pipeline.
    role_family: str = "other"
    seniority: str = "unknown"
    score: float = 0.0
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    # Structured hints some sources give us directly (jobs.ch does).
    required_languages: list[str] = field(default_factory=list)
    min_years_experience: int | None = None

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Stable identity: same source + same source key => same row forever."""
        return hashlib.sha1(
            f"{self.source}:{self.source_id}".encode("utf-8")
        ).hexdigest()[:20]

    @property
    def dedupe_key(self) -> str:
        """Cross-source identity. Same role at same employer collapses to one row."""
        return hashlib.sha1(
            f"{normalize_company(self.company)}|{normalize_title(self.title)}|"
            f"{_slug(self.city)}".encode("utf-8")
        ).hexdigest()[:20]

    def snippet(self, n: int = 400) -> str:
        text = re.sub(r"\s+", " ", self.description or "").strip()
        return text[:n] + ("..." if len(text) > n else "")

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        d["id"] = self.id
        d["dedupe_key"] = self.dedupe_key
        # Truncated on the way to storage only, so the scorer still sees the
        # whole posting.
        if d.get("description") and len(d["description"]) > MAX_DESCRIPTION_CHARS:
            d["description"] = d["description"][:MAX_DESCRIPTION_CHARS] + "\n[truncated]"
        return d


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: Longest description we persist. Bounding this keeps the database compact even
#: when a source returns unusually large posting bodies.
#:
#: Scoring is unaffected: it runs on the in-memory Job before storage, and
#: every consumer of the stored text works well inside this bound - the
#: dashboard shows 1,500 chars, language detection reads 4,000, and the
#: experience-year parser reads 12,000 but the requirement is stated far
#: earlier in practice. Only free-text search over the tail is lost.
MAX_DESCRIPTION_CHARS = 8000
