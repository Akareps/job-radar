"""Interactive setup for a local, candidate-specific job search.

The wizard accepts arbitrary job titles. Optional CV extraction only supplies
editable defaults; every value that reaches the generated files is presented
to the user first.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from . import config as cfg


SENIORITY_MENU = [
    ("junior", "Graduate / junior (0-2 years)"),
    ("mid", "Mid-level (2-5 years)"),
    ("senior", "Senior (5+ years)"),
]


def ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{question}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def ask_yes(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = ask(f"{question} ({hint})").lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n.")


def ask_int(question: str, default: int) -> int:
    while True:
        raw = ask(question, str(default))
        try:
            return int(raw)
        except ValueError:
            print("  Please enter a whole number.")


def ask_list(question: str, default: list[str]) -> list[str]:
    raw = ask(f"{question} (comma-separated)", ", ".join(default))
    return [part.strip() for part in raw.split(",") if part.strip()]


def ask_semicolon_list(question: str, default: list[str] | None = None) -> list[str]:
    """Read values whose contents may themselves contain commas."""
    raw = ask(f"{question} (semicolon-separated)", "; ".join(default or []))
    return [part.strip() for part in raw.split(";") if part.strip()]


def ask_choice(question: str, options: list[tuple], default_keys: list[str]) -> list[str]:
    print(f"\n{question}")
    for index, option in enumerate(options, 1):
        print(f"  {index}. {option[1]}")
    default = ",".join(
        str(i) for i, option in enumerate(options, 1) if option[0] in default_keys
    ) or "1"
    while True:
        raw = ask("  Numbers, in priority order", default)
        try:
            picked = [options[int(value.strip()) - 1]
                      for value in raw.split(",") if value.strip()]
        except (ValueError, IndexError):
            print("  Please give numbers from the list, e.g. 1,3")
            continue
        if picked:
            return [option[0] for option in picked]
        print("  Pick at least one.")


def run(force: bool = False, cv_path: str | None = None) -> int:
    root = cfg.ROOT
    profile_path = root / "config" / "profile.yaml"
    settings_path = root / "config" / "settings.yaml"
    example = root / "config" / "profile.example.yaml"

    if profile_path.exists() and not force:
        print(f"{profile_path} already exists. Re-run with --force to overwrite it.")
        return 1
    if not example.exists():
        print(f"Missing {example} - is the checkout complete?")
        return 1

    print("\n  jobradar setup")
    print("  " + "-" * 52)
    print("  A few questions, then you get a working local configuration.")
    print("  Everything is written to config/ and can be edited afterwards.\n")

    hints = _maybe_read_cv(cv_path)

    name = ask("Your name", hints.name if hints else "")
    languages = ask_list(
        "Languages you can work in, as codes (en, de, fr, es, ...)",
        hints.languages if hints and hints.languages else ["en"],
    )

    print()
    home = ask(
        "Which city do you want to work in? (e.g. 'Berlin, Germany')",
        hints.current_location if hints else "",
    )
    while not home:
        print("  This one is required - it anchors every distance calculation.")
        home = ask("Home city")
    radius = ask_int("How far would you commute, in km", 40)
    region = ask_int("How far would you relocate within the region, in km", 150)
    hubs = ask_semicolon_list(
        "Other cities you would move for (e.g. 'London, UK; Amsterdam, NL')"
    )

    default_titles = hints.target_roles if hints and hints.target_roles else ["Software Engineer"]
    target_titles = ask_semicolon_list("Target job titles", default_titles)
    while not target_titles:
        print("  Enter at least one title. These become both search queries and ranking rules.")
        target_titles = ask_semicolon_list("Target job titles")

    default_level = _default_seniority(hints.years_experience if hints else 0)
    seniority = ask_choice("What level?", SENIORITY_MENU, [default_level])[0]

    degree_default = hints.degree_level if hints and hints.degree_level else "other"
    degree = ask("Highest degree (bsc, msc, phd, other)", degree_default).lower()
    while degree not in {"bsc", "msc", "phd", "other"}:
        print("  Please enter bsc, msc, phd, or other.")
        degree = ask("Highest degree", degree_default).lower()
    graduating = ask(
        "Graduation month, YYYY-MM (blank if not applicable)",
        hints.graduating if hints else "",
    )

    high = ask_list("Core skills", hints.skills_high if hints else [])
    medium = ask_list("Working skills", hints.skills_medium if hints else [])
    low = ask_list("Other useful skills", hints.skills_low if hints else [])
    target_companies = ask_list("Target companies (blank for none)", [])

    profile = yaml.safe_load(example.read_text(encoding="utf-8"))
    _apply(
        profile, name, languages, home, radius, region, hubs, target_titles,
        seniority, degree, graduating, high, medium, low, target_companies,
    )

    header = (
        "# jobradar matching profile - generated by `jobradar init`.\n"
        "# Edit freely, then run `jobradar rescore` to re-rank without rescanning.\n"
        "# The fully commented reference lives in config/profile.example.yaml.\n"
    )
    profile_path.write_text(
        header + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )

    if not settings_path.exists() or force:
        _write_settings(settings_path, root, home, hubs, target_titles)
    copy_example_if_missing("seed_companies")

    print("\n  " + "-" * 52)
    print(f"  Wrote {profile_path.relative_to(root)}")
    print(f"  Wrote {settings_path.relative_to(root)}")
    print("  Seeded config/seed_companies.yaml (edit it before discovery).")
    print("\n  Next:")
    print("    jobradar geo-update     download the city dataset (~5 MB, once)")
    print("    jobradar discover       find which ATS each seed company uses")
    print("    jobradar scan           first scan")
    print("    jobradar serve          open the dashboard\n")
    return 0


def _maybe_read_cv(cv_path: str | None):
    from . import cvparse

    usable, note = cvparse.available()
    if not usable:
        if cv_path:
            print(f"  Cannot read the CV: {note}\n")
        return None

    if not cv_path:
        if not ask_yes("Read a CV to pre-fill these answers?", default=False):
            return None
        cv_path = ask("Path to your CV (PDF, .txt or .md)")
        if not cv_path:
            return None

    print("  CV extraction sends the document text to Anthropic's API.")
    if not ask_yes("Continue and send this CV for extraction?", default=False):
        print("  CV extraction skipped.\n")
        return None
    if note:
        print(f"  note: {note}")
    print("  Reading the CV...")
    try:
        hints = cvparse.from_cv(cv_path.strip().strip('"'))
    except Exception as exc:  # noqa: BLE001 - never let this block setup
        print(f"  Could not use the CV: {exc}")
        print("  Continuing with the questions instead.\n")
        return None

    if hints.empty:
        print("  Nothing usable came back; continuing with the questions.\n")
        return None

    print(f"  Suggested roles: {', '.join(hints.target_roles[:4]) or '-'}")
    print(f"  Core skills: {', '.join(hints.skills_high[:8]) or '-'}")
    print("  Review or replace each suggestion below.\n")
    return hints


def _default_seniority(years_experience: int) -> str:
    if years_experience >= 5:
        return "senior"
    if years_experience >= 2:
        return "mid"
    return "junior"


def _guess_families(hints) -> list[str]:
    """Related built-in templates to retain near the top, never a default role."""
    blob = f" {' '.join(hints.target_roles + hints.skills_high).lower()} "
    picked = []
    if any(word in blob for word in ("quant", "trading", "trader", "alpha")):
        picked.extend(["quant_trading", "quant_dev", "quant_research"])
    if any(word in blob for word in ("machine learning", " ml ", "deep learning", " ai ", "nlp")):
        picked.append("ml_engineer")
    if any(word in blob for word in ("data scien", "analytics", "statist")):
        picked.extend(["data_scientist", "data_engineer"])
    if any(word in blob for word in ("software", "developer", "backend", "full stack")):
        picked.append("software_engineer")
    return list(dict.fromkeys(picked))


#: Explicit grade prefixes must remain visible to seniority classification.
#: Only the ambiguous role phrase that follows may be neutralised.
_EXPLICIT_GRADE_PREFIX = re.compile(
    r"^\s*(senior|sr\.?|lead|principal|head|chief|director|expert|advanced|"
    r"experienced)\b[\s,:/\-]*", re.I,
)

#: `staff` is level-neutral in these professions but senior in technology.
_NEUTRAL_STAFF_ROLE = re.compile(
    r"\bstaff\s+(?:(?:registered|tax|financial|cost|general)\s+)?"
    r"(nurse|accountant)\b", re.I,
)


def _title_pattern(title: str) -> str:
    """Literal, case-insensitive-friendly title match with flexible whitespace."""
    escaped = re.escape(" ".join(title.split())).replace(r"\ ", r"\s+")
    return rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"


def _seniority_exempt_pattern(title: str) -> str | None:
    """The level-neutral role phrase inside an explicitly requested title.

    Examples: `Staff Nurse` and `Product Manager` are role names, while the
    prefixes in `Senior Staff Nurse` and `Lead Product Manager` remain grades.
    `Staff Software Engineer` is not neutralised because staff is a real senior
    engineering grade.
    """
    role = " ".join(title.split())
    while True:
        reduced, count = _EXPLICIT_GRADE_PREFIX.subn("", role, count=1)
        if not count:
            break
        role = reduced.strip()

    words = role.split()
    is_manager_role = len(words) > 1 and words[-1].lower() == "manager"
    if _NEUTRAL_STAFF_ROLE.search(role) or is_manager_role:
        return _title_pattern(role)
    return None


def _skill_pattern(skill: str) -> str:
    """Treat wizard and CV skill names as literals, not executable regex."""
    return re.escape(" ".join(skill.split())).replace(r"\ ", r"\s+")


def _apply(
    profile: dict, name: str, languages: list[str], home: str, radius: int,
    region: int, hubs: list[str], target_titles: list[str], seniority: str,
    degree: str, graduating: str, skills_high: list[str],
    skills_medium: list[str], skills_low: list[str], target_companies: list[str],
) -> None:
    candidate = profile["candidate"]
    candidate["name"] = name or "You"
    candidate["languages"] = languages or ["en"]
    candidate["degree_level"] = degree
    if graduating:
        candidate["graduating"] = graduating
    else:
        candidate.pop("graduating", None)

    location: dict[str, Any] = profile["location"]
    location["home"] = home
    location["bands"] = [
        {"within_km": radius, "tier": "local", "points": 32},
        {"within_km": region, "tier": "regional", "points": -6},
    ]
    location["also_good"] = [
        {"place": hub, "tier": "hub", "points": 16, "within_km": 40}
        for hub in hubs
    ]
    location["name_hints"] = {
        "local": [home.split(",")[0].strip().lower()],
        "hub": [hub.split(",")[0].strip().lower() for hub in hubs],
    }

    families = profile.get("role_families", {})
    custom = {
        "weight": 100,
        "label": "Target roles",
        "patterns": [_title_pattern(title) for title in target_titles],
    }
    hint = type("RoleHints", (), {"target_roles": target_titles, "skills_high": skills_high})
    related = [key for key in _guess_families(hint) if key in families]
    rest = [key for key in families if key not in related and key != "target_roles"]
    profile["role_families"] = {
        "target_roles": custom,
        **{key: families[key] for key in related + rest},
    }
    for key in rest:
        profile["role_families"][key]["weight"] = min(
            profile["role_families"][key]["weight"], 35
        )

    profile["exclude_titles"] = [
        pattern for pattern in profile.get("exclude_titles", [])
        if not any(re.search(pattern, title, re.I) for title in target_titles)
    ]

    # Store only ambiguous role phrases, never an explicit grade. Thus "Staff
    # Nurse" can be neutral while "Senior Staff Nurse" still reads as senior,
    # and technology's "Staff Software Engineer" remains a senior title.
    exempt_patterns = [_seniority_exempt_pattern(title) for title in target_titles]
    profile["scoring"]["seniority_exempt_titles"] = [
        pattern for pattern in exempt_patterns if pattern
    ]
    profile["scoring"]["target_seniority"] = seniority

    caps = profile["scoring"]["hard_caps"]
    if seniority == "mid":
        profile["scoring"]["seniority"].update({"junior": 10, "mid": 20, "unknown": 8})
        caps["senior"] = 70
    elif seniority == "senior":
        profile["scoring"]["seniority"].update(
            {"junior": -20, "mid": 10, "unknown": 8, "senior": 20}
        )
        caps["senior"] = 999
        profile["scoring"]["years_experience"] = {0: -10, 2: 0, 5: 12, 8: 14}

    profile["skills"] = {
        "high": [_skill_pattern(skill) for skill in skills_high],
        "medium": [_skill_pattern(skill) for skill in skills_medium],
        "low": [_skill_pattern(skill) for skill in skills_low],
    }
    profile["company_tiers"] = {"A": target_companies, "B": []}
    profile.setdefault("pay_bands", {})["companies"] = {}


def _write_settings(
    path: Path, root: Path, home: str, hubs: list[str], target_titles: list[str]
) -> None:
    example = root / "config" / "settings.example.yaml"
    settings = yaml.safe_load(example.read_text(encoding="utf-8")) if example.exists() else {}

    settings["queries"] = list(dict.fromkeys(target_titles))
    cities = [home] + hubs
    settings["locations"] = cities
    boards = settings.setdefault("boards", {})
    boards.setdefault("linkedin", {})["locations"] = cities
    boards.setdefault("amazon", {})["locations"] = cities

    # Adzuna needs explicit supported country codes, which cannot be derived
    # reliably from free-form city names. Keep it off until the user configures it.
    adzuna = boards.setdefault("adzuna", {})
    adzuna["enabled"] = False
    adzuna["countries"] = []
    adzuna["where"] = {}

    header = (
        "# Which sources run and what they search for.\n"
        "# Generated by `jobradar init`; see config/settings.example.yaml for\n"
        "# every option with comments.\n"
    )
    path.write_text(
        header + yaml.safe_dump(settings, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )


def copy_example_if_missing(name: str) -> None:
    """Seed config/<name>.yaml from its .example.yaml when absent."""
    target = cfg.ROOT / "config" / f"{name}.yaml"
    example = cfg.ROOT / "config" / f"{name}.example.yaml"
    if not target.exists() and example.exists():
        shutil.copy(example, target)
