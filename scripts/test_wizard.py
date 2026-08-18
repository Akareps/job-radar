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
    _apply, _default_seniority, _guess_families, _seniority_exempt_pattern,
    _skill_pattern, _write_settings,
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

# --- ambiguous role words are neutral, explicit grades remain grades --------
graded = [
    "Staff Nurse", "Staff Accountant", "Product Manager", "Nurse Manager",
    "Senior Care Assistant", "Senior Software Engineer", "Staff Software Engineer",
    "Head Chef",
]
graded_profile = yaml.safe_load(
    (ROOT / "config" / "profile.example.yaml").read_text(encoding="utf-8"))
_apply(graded_profile, "N", ["en"], "Manchester, United Kingdom", 30, 120, [],
       graded, "mid", "bsc", "", ["patient care"], [], [], [])
exempt = graded_profile["scoring"]["seniority_exempt_titles"]
check("only ambiguous role phrases are exempted", len(exempt), 4)
check("chosen target level is stored", graded_profile["scoring"]["target_seniority"], "mid")
check("Staff Nurse is neutral", _seniority_exempt_pattern("Staff Nurse") is not None, True)
check("Product Manager is neutral", _seniority_exempt_pattern("Product Manager") is not None, True)
check("Senior prefix is excluded from neutral phrase",
      "Senior" not in (_seniority_exempt_pattern("Senior Product Manager") or ""), True)
check("tech Staff remains a grade",
      _seniority_exempt_pattern("Staff Software Engineer"), None)
check("explicit Senior remains a grade",
      _seniority_exempt_pattern("Senior Software Engineer"), None)
check("bare Manager remains a grade", _seniority_exempt_pattern("Manager"), None)

graded_path = tmp / "graded.yaml"
graded_path.write_text(yaml.safe_dump(graded_profile, sort_keys=False), encoding="utf-8")
graded_scorer = Scorer(graded_path)


def seniority_of(title, description=""):
    job = Job(source="s", source_id="1", title=title, company="NHS",
              url="u", location="Manchester, United Kingdom", description=description)
    return graded_scorer.score(job).seniority


check("Staff Nurse uses chosen mid level", seniority_of("Staff Nurse"), "mid")
check("suffixed Staff Nurse uses chosen mid level",
      seniority_of("Staff Nurse - Emergency Department"), "mid")
check("Staff Accountant uses chosen mid level", seniority_of("Staff Accountant"), "mid")
check("Product Manager uses chosen mid level", seniority_of("Product Manager"), "mid")
check("Nurse Manager uses chosen mid level", seniority_of("Nurse Manager"), "mid")
check("Senior Staff Nurse keeps the explicit grade",
      seniority_of("Senior Staff Nurse"), "senior")
check("Senior Product Manager keeps the explicit grade",
      seniority_of("Senior Product Manager"), "senior")
check("Senior Nurse Manager keeps the explicit grade",
      seniority_of("Senior Nurse Manager"), "senior")
check("asked-for Senior Care Assistant stays senior",
      seniority_of("Senior Care Assistant"), "senior")
check("asked-for Senior Software Engineer stays senior",
      seniority_of("Senior Software Engineer"), "senior")
check("technology Staff Software Engineer stays senior",
      seniority_of("Staff Software Engineer"), "senior")
check("Head Chef stays senior", seniority_of("Head Chef"), "senior")
check("stated experience overrides the target fallback",
      seniority_of("Staff Nurse", "At least 5 years of experience required."), "senior")
check("junior body language overrides the target fallback",
      seniority_of("Staff Nurse", "Graduate and early career applicants are welcome."), "junior")


if failures:
    print(f"FAILED {len(failures)} check(s):\n")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("All generic-wizard checks passed.")
