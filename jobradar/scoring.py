"""Fit scoring. Every component is recorded so the dashboard can explain a rank.

Nothing here is a black box on purpose: if a job ranks oddly you can read the
breakdown, edit config/profile.yaml, and run `python -m jobradar rescore`.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .geo import load_index
from .location import LocationClassifier
from .models import Job
from .text import (
    age_days,
    classify_seniority,
    detect_language,
    parse_min_years,
    requires_language,
    requires_phd,
)

_CLEARANCE = re.compile(
    r"(security\s+clearance|us\s+citizen(ship)?\s+(required|only)|"
    r"must\s+be\s+a\s+u\.?s\.?\s+citizen|ts/sci|polygraph)",
    re.I,
)

#: Doctoral positions remain visible, but rank below comparable industry roles.
_PHD_POSITION = re.compile(
    r"\b(ph\.?d\.?[\s-]*(candidate|student|position|fellow|researcher)|"
    r"doctoral\s+(candidate|student|position|researcher)|doktorand)",
    re.I,
)

#: Titles that describe a hiring programme rather than a role. Only these are
#: allowed to have their family inferred from the description body.
#: Bare "program" is deliberately excluded: a generic program-management title
#: can otherwise inherit an unrelated role family from a stray body keyword.
_GENERIC_PROGRAM = re.compile(
    r"\b(graduate|campus|new\s+grad|early\s+career|rotational|trainee|"
    r"internship|intern|apprentice|talent\s+pool|open\s+application|speculative)\b"
    r"|\b(graduate|campus|rotational|talent|analyst)\s+programme?\b",
    re.I,
)

#: A postdoc is by definition gated behind a doctorate.
_POSTDOC = re.compile(r"\b(post[\s-]?doc(toral)?|postdoktor)", re.I)


def fold(name: str) -> str:
    """Lowercase, strip diacritics, collapse to single-spaced alphanumerics.

    Swiss employer names are full of umlauts and the tier lists spell them
    both ways. Without folding, dropping non-ASCII turned "Zürcher" into
    "z rcher", which quietly cost Zürcher Kantonalbank, Zühlke, Universität
    Zürich and ETH Zürich their company tier and pay band. Applied to the
    config needles as well, so either spelling works on either side.
    """
    text = unicodedata.normalize("NFKD", (name or "").lower().replace("ß", "ss"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


@lru_cache(maxsize=4)
def load_profile(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


#: Languages we can meaningfully test a requirement for. Anything outside this
#: set is simply not checked rather than guessed at.
CHECKABLE_LANGUAGES = ("de", "fr", "es", "it", "nl", "pt", "pl", "sv")


class Scorer:
    def __init__(self, profile_path: str | Path, city_cache: str | Path | None = None):
        self.profile = load_profile(str(profile_path))
        self.families = self.profile["role_families"]
        self.cfg = self.profile["scoring"]
        self.skills = self.profile["skills"]

        candidate = self.profile.get("candidate", {}) or {}
        self.spoken = {str(c).lower()[:2] for c in candidate.get("languages", ["en"])}
        self.spoken.add("en")  # every posting we can read at all is readable
        self.checkable_languages = [c for c in CHECKABLE_LANGUAGES if c not in self.spoken]

        cache = str(city_cache) if city_cache else str(
            Path(profile_path).resolve().parent.parent / "data" / "cities.tsv.gz")
        self.location = LocationClassifier(
            self.profile.get("location", {}) or {}, load_index(cache)
        )

        self._family_res = {
            name: [re.compile(p, re.I) for p in spec["patterns"]]
            for name, spec in self.families.items()
        }
        self._exclude_res = [
            re.compile(p, re.I) for p in self.profile.get("exclude_titles", [])
        ]
        self._skill_res = {
            level: [(s, re.compile(rf"(?<![a-z0-9]){s}(?![a-z0-9])", re.I))
                    for s in self.skills.get(level, [])]
            for level in ("high", "medium", "low")
        }
        # Needles are folded at load time so config can spell "Zürcher" or
        # "Zurcher" and either matches either form in a posting.
        tiers = self.profile.get("company_tiers", {})
        self._company_tiers = [
            (tier, [fold(c) for c in tiers.get(tier, [])]) for tier in ("A", "B")
        ]
        pay = self.profile.get("pay_bands", {}) or {}
        self._pay_default = int(pay.get("default", 4))
        self._pay_companies = [
            (int(band), [fold(c) for c in names])
            for band, names in sorted((pay.get("companies") or {}).items())
        ]
        self._pay_pts = {int(k): float(v)
                         for k, v in (self.cfg.get("pay_band_pts") or {}).items()}
        self._caps = {k: float(v) for k, v in (self.cfg.get("hard_caps") or {}).items()}

        # Level-neutral role phrases selected by the user. The wizard excludes
        # explicit modifiers, so "Senior Staff Nurse" retains "Senior" after
        # the neutral "Staff Nurse" phrase is removed.
        self._seniority_exempt = [
            re.compile(pattern, re.I)
            for pattern in (self.cfg.get("seniority_exempt_titles") or [])
        ]
        target_level = str(self.cfg.get("target_seniority") or "").lower()
        self._target_seniority = (
            target_level if target_level in {"junior", "mid", "senior"} else ""
        )

    # ------------------------------------------------------------------ #

    def classify_role(self, title: str, description: str = "") -> str:
        """First family whose pattern hits the title wins; families are ordered
        best-first in the config, so a 'Quant Research' hit beats 'Data Scientist'."""
        for name in self.families:
            for rx in self._family_res[name]:
                if rx.search(title):
                    return name
        # Only fall back to the description for titles that name a programme
        # rather than a role ("Graduate Programme 2027", "Campus Hire").
        # Matching any short title against the body misfires badly - an OpenAI
        # posting called "Industrial Compute" came out as research_scientist.
        if _GENERIC_PROGRAM.search(title):
            head = description[:800]
            for name in self.families:
                for rx in self._family_res[name]:
                    if rx.search(head):
                        return name
        return "other"

    @staticmethod
    def _tokenised(company: str) -> str:
        """Fold and pad a company name so tier needles match whole tokens."""
        return f" {fold(company)} "

    def company_tier(self, company: str) -> str:
        padded = self._tokenised(company)
        for tier, needles in self._company_tiers:
            if any(f" {n} " in padded for n in needles):
                return tier
        return "C"

    def pay_band(self, company: str) -> int:
        """1 (best paid) .. 4. Estimates from profile.yaml, not real offers."""
        padded = self._tokenised(company)
        for band, needles in self._pay_companies:
            if any(f" {n} " in padded for n in needles):
                return band
        return self._pay_default

    def is_excluded(self, title: str) -> bool:
        return any(rx.search(title or "") for rx in self._exclude_res)

    def _skill_score(self, blob: str) -> tuple[float, list[str]]:
        points = {"high": 3.0, "medium": 2.0, "low": 1.0}
        total, hits = 0.0, []
        for level, entries in self._skill_res.items():
            for name, rx in entries:
                if rx.search(blob):
                    total += points[level]
                    hits.append(name.replace("\\", ""))
        return min(total, float(self.cfg["skill_bonus_cap"])), hits

    @staticmethod
    def _step(table: dict, key: float) -> float:
        """Pick the value for the smallest threshold >= key (recency/years tables)."""
        for threshold in sorted(int(k) for k in table):
            if key <= threshold:
                return float(table[threshold])
        return float(table[max(int(k) for k in table)])

    # ------------------------------------------------------------------ #

    def score(self, job: Job) -> Job:
        """Fills in role_family, seniority, language, city/country, score, breakdown."""
        title = job.title or ""
        desc = job.description or ""
        blob = f"{title}\n{desc}"
        parts: dict[str, Any] = {}

        # --- role family -------------------------------------------------
        family = self.classify_role(title, desc)
        job.role_family = family
        base = float(self.families[family]["weight"]) if family != "other" else 15.0
        parts["role_family"] = family
        parts["base"] = base

        # --- seniority ---------------------------------------------------
        years = job.min_years_experience
        if years is None:
            years = parse_min_years(desc)
            job.min_years_experience = years
        # Remove only the ambiguous role phrase. Explicit modifiers left in the
        # title, stated years, and junior language in the body still win. If no
        # other signal exists, use the level the candidate selected in setup.
        probe = title
        matched_exempt = False
        for exempt in self._seniority_exempt:
            probe, count = exempt.subn(" ", probe)
            matched_exempt = matched_exempt or count > 0
        seniority = classify_seniority(probe, desc, years)
        if matched_exempt and seniority == "unknown" and self._target_seniority:
            seniority = self._target_seniority
        job.seniority = seniority
        sen_pts = float(self.cfg["seniority"].get(seniority, 0))
        parts["seniority"] = seniority
        parts["seniority_pts"] = sen_pts

        yr_pts = self._step(self.cfg["years_experience"], years) if years is not None else 0.0
        parts["min_years"] = years
        parts["years_pts"] = yr_pts

        # --- location ----------------------------------------------------
        verdict = self.location.classify(job.location, job.country, job.remote)
        job.city = verdict.city or job.city
        job.country = verdict.country or job.country
        loc_pts = verdict.points
        parts["location_tier"] = verdict.tier
        parts["location_pts"] = loc_pts
        if verdict.distance_km is not None:
            parts["distance_km"] = verdict.distance_km

        # --- recency -----------------------------------------------------
        age = age_days(job.posted_at)
        rec_pts = self._step(self.cfg["recency"], age) if age is not None else 0.0
        parts["age_days"] = round(age, 1) if age is not None else None
        parts["recency_pts"] = rec_pts

        # Some employers keep pipeline requisitions open for years. They may be
        # worth applying to, but they are not new openings.
        if age is not None and age > 180 and job.source.startswith("ats:"):
            job.tags.append("evergreen")

        # --- language ----------------------------------------------------
        # Penalise only languages the candidate does not read. A German-language
        # posting is a problem for a non-German speaker and a non-event for
        # everyone else, so this is driven by candidate.languages.
        lang = job.language or detect_language(title, desc)
        job.language = lang
        lang_cfg = self.cfg.get("language", {})
        lang_pts = 0.0

        # Some boards (jobs.ch among them) publish structured language
        # requirements; trust those over anything parsed out of prose.
        declared = {code.lower()[:2] for code in job.required_languages}
        missing_declared = declared - self.spoken

        required_missing = sorted(missing_declared) or [
            code for code in self.checkable_languages
            if requires_language(blob, code)
        ]
        if required_missing:
            lang_pts += float(lang_cfg.get("required_missing", -30))
            job.tags.append(f"{required_missing[0]}-required")
        elif lang not in self.spoken:
            lang_pts += float(lang_cfg.get("posting_missing", -9))
            job.tags.append(f"{lang}-posting")

        parts["language"] = lang
        parts["language_pts"] = lang_pts

        # --- company -----------------------------------------------------
        ctier = self.company_tier(job.company)
        comp_pts = float(self.cfg["company_tier"].get(ctier, 0))
        parts["company_tier"] = ctier
        parts["company_pts"] = comp_pts
        if ctier == "A":
            job.tags.append("target-company")

        # --- pay ---------------------------------------------------------
        # Recorded but NOT added to the balanced total: company tier already
        # rewards the same employers, and double-counting would distort the
        # default order. The dashboard's "best paid" mode weights this up.
        band = self.pay_band(job.company)
        parts["pay_band"] = band
        parts["pay_pts"] = self._pay_pts.get(band, 0.0)

        # --- skills ------------------------------------------------------
        skill_pts, skill_hits = self._skill_score(blob)
        parts["skill_pts"] = skill_pts
        parts["skill_hits"] = skill_hits[:14]

        # --- hard penalties ----------------------------------------------
        pen = 0.0
        if requires_phd(desc) or _POSTDOC.search(title):
            pen += float(self.cfg["penalties"]["phd_required"])
            job.tags.append("phd-required")
        if _CLEARANCE.search(desc):
            pen += float(self.cfg["penalties"]["clearance_required"])
            job.tags.append("clearance")
        if _PHD_POSITION.search(title):
            pen += float(self.cfg["penalties"].get("phd_position", -15))
            job.tags.append("phd-position")
        parts["penalty_pts"] = pen

        # --- total -------------------------------------------------------
        # Adjustments scale the role weight rather than adding to it, keeping
        # bonuses proportional to the role's relevance in the first place.
        adjustments = (sen_pts + yr_pts + loc_pts + rec_pts
                       + lang_pts + comp_pts + skill_pts + pen)
        multiplier = max(0.05, 1.0 + adjustments / 100.0)
        total = base * multiplier

        parts["adjustments"] = round(adjustments, 1)
        parts["multiplier"] = round(multiplier, 3)

        if self.is_excluded(title):
            parts["excluded"] = True
            job.tags.append("off-target")

        # --- hard caps ---------------------------------------------------
        # Scaling alone can let senior or credential-gated postings outrank
        # realistically attainable roles. These ceilings are absolute.
        capped_by = self._apply_caps(job, seniority)
        if capped_by:
            ceiling = min(self._caps[name] for name in capped_by)
            if total > ceiling:
                parts["capped_by"] = sorted(capped_by)
                parts["cap_ceiling"] = ceiling
                parts["uncapped_score"] = round(total, 1)
                total = ceiling

        job.score = round(max(total, 0.0), 1)
        job.score_breakdown = parts
        job.tags = sorted(set(job.tags))
        return job

    def _apply_caps(self, job: Job, seniority: str) -> set[str]:
        """Which hard caps this posting trips."""
        tripped: set[str] = set()
        if seniority == "senior" and "senior" in self._caps:
            tripped.add("senior")
        if seniority == "working_student" and "working_student" in self._caps:
            tripped.add("working_student")
        for tag, cap in (("phd-required", "phd_required"),
                         ("phd-position", "phd_position"),
                         ("clearance", "clearance"),
                         ("off-target", "off_target")):
            if tag in job.tags and cap in self._caps:
                tripped.add(cap)
        return tripped


def rescore_db(store, profile_path: str | Path, ids: list[str] | None = None) -> int:
    """Re-run scoring over stored rows against this machine's profile.

    `ids` restricts the pass to specific rows, which is what a cloud sync needs:
    the merged rows carry whatever the remote's profile produced, and only a
    local rescore makes them consistent with the rest of the database.
    """
    import json

    scorer = Scorer(profile_path)
    if ids is None:
        rows = store.conn.execute("SELECT * FROM jobs").fetchall()
    elif not ids:
        return 0
    else:
        rows = []
        # Chunked to stay under SQLite's variable limit on a big merge.
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows += store.conn.execute(
                f"SELECT * FROM jobs WHERE id IN ({placeholders})", chunk
            ).fetchall()
    updated = 0
    for row in rows:
        job = Job(
            source=row["source"], source_id=row["source_id"], title=row["title"],
            company=row["company"], url=row["url"] or "", location=row["location"] or "",
            country=row["country"] or "", description=row["description"] or "",
            posted_at=row["posted_at"], language="",
            remote=bool(row["remote"]),
            required_languages=json.loads(row["required_languages"] or "[]"),
            min_years_experience=row["min_years_experience"],
        )
        scorer.score(job)
        # dedupe_key must be recomputed too: it is derived from the company and
        # title normalisers, so a change to those only takes effect here.
        store.conn.execute(
            "UPDATE jobs SET score=?, score_breakdown=?, role_family=?, seniority=?, "
            "language=?, city=?, country=?, tags=?, dedupe_key=? WHERE id=?",
            (job.score, json.dumps(job.score_breakdown, ensure_ascii=False),
             job.role_family, job.seniority, job.language, job.city, job.country,
             json.dumps(job.tags, ensure_ascii=False), job.dedupe_key, row["id"]),
        )
        updated += 1
    store.conn.commit()
    return updated
