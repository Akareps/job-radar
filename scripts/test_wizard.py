"""Regression tests for profession-neutral configuration generation."""

import re
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobradar.models import Job
from jobradar.scoring import Scorer  # noqa: E402
from jobradar.wizard import (  # noqa: E402
    _apply, _default_seniority, _guess_families, _skill_pattern, _write_settings,
)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")


titles = ["Accountant", "Registered Nurse", "Mechanical Engineer", "Product Manager"]
profile = yaml.safe_load((ROOT / "config" / "profile.example.yaml").read_text(encoding="utf-8"))
profile["exclude_titles"] = ["accountant|audit", "nurse|physician", "sales"]
_apply(
    profile, "Example Person", ["en", "de"], "Paris, France", 30, 120,
    ["Lyon, France"], titles, "mid", "bsc", "2027-06",
    ["planning"], ["excel"], ["git"], ["Example Company"],
)

check("custom family comes first", next(iter(profile["role_families"])), "target_roles")
patterns = [re.compile(p, re.I) for p in profile["role_families"]["target_roles"]["patterns"]]
for title in titles:
    check(f"target pattern matches {title}", any(rx.search(title) for rx in patterns), True)
    check(
        f"selected title is not excluded: {title}",
        any(re.search(pattern, title, re.I) for pattern in profile["exclude_titles"]),
        False,
    )

check("unrelated exclusion remains", profile["exclude_titles"], ["sales"])
check("explicit core skills used", profile["skills"]["high"], ["planning"])
check("skill regex characters escaped", _skill_pattern("C++"), r"C\+\+")
check("explicit graduation used", profile["candidate"]["graduating"], "2027-06")
check("target companies used", profile["company_tiers"]["A"], ["Example Company"])
check("pay assumptions cleared", profile["pay_bands"]["companies"], {})
check("2 years defaults to mid", _default_seniority(2), "mid")
check("5 years defaults to senior", _default_seniority(5), "senior")

plain_hints = type("Hints", (), {"target_roles": ["Accountant"], "skills_high": ["tax"]})
check("unknown profession has no tech fallback", _guess_families(plain_hints), [])

tmp = Path(tempfile.mkdtemp())
(tmp / "config").mkdir()
(tmp / "config" / "settings.example.yaml").write_text(
    (ROOT / "config" / "settings.example.yaml").read_text(encoding="utf-8"),
    encoding="utf-8",
)
settings_path = tmp / "config" / "settings.yaml"
_write_settings(settings_path, tmp, "Paris, France", ["Lyon, France"], titles)
settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
check("titles become exact queries", settings["queries"], titles)
check("locations are generated", settings["locations"], ["Paris, France", "Lyon, France"])
check("amazon locations updated", settings["boards"]["amazon"]["locations"], settings["locations"])
check("linkedin locations updated", settings["boards"]["linkedin"]["locations"], settings["locations"])
check("Adzuna remains opt-in", settings["boards"]["adzuna"]["enabled"], False)
check("Adzuna countries not guessed", settings["boards"]["adzuna"]["countries"], [])
check("Adzuna locations not stale", settings["boards"]["adzuna"]["where"], {})

profile_path = tmp / "profile.yaml"
profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
scorer = Scorer(profile_path)
for title in titles:
    check(f"scorer accepts arbitrary target {title}", scorer.classify_role(title), "target_roles")

# --- a grade word inside a title the user asked for is not a grade -----------
# "Staff Nurse" is the standard UK nursing band and "Staff Accountant" the
# junior US accounting one, but `staff` reads as senior in tech. Without the
# exemption every result for those searches was classified senior and capped.
graded = ["Staff Nurse", "Senior Care Assistant", "Registered Nurse"]
graded_profile = yaml.safe_load(
    (ROOT / "config" / "profile.example.yaml").read_text(encoding="utf-8"))
_apply(graded_profile, "N", ["en"], "Manchester, United Kingdom", 30, 120, [],
       graded, "mid", "bsc", "", ["patient care"], [], [], [])
exempt = graded_profile["scoring"]["seniority_exempt_titles"]
check("only graded titles are exempted", len(exempt), 2)

graded_path = tmp / "graded.yaml"
graded_path.write_text(yaml.safe_dump(graded_profile, sort_keys=False), encoding="utf-8")
graded_scorer = Scorer(graded_path)


def seniority_of(title):
    job = Job(source="s", source_id="1", title=title, company="NHS",
              url="u", location="Manchester, United Kingdom")
    return graded_scorer.score(job).seniority


check("asked-for Staff Nurse is not senior", seniority_of("Staff Nurse"), "unknown")
check("suffixed Staff Nurse is not senior",
      seniority_of("Staff Nurse - Emergency Department"), "unknown")
check("asked-for Senior Care Assistant is not senior",
      seniority_of("Senior Care Assistant"), "unknown")
# Subtracting the exempt title rather than skipping keeps real grades readable.
check("a genuine grade on top still counts",
      seniority_of("Senior Staff Nurse"), "senior")
check("an unrelated senior title still counts",
      seniority_of("Nurse Manager"), "senior")


if failures:
    print(f"FAILED {len(failures)} check(s):\n")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("All generic-wizard checks passed.")
