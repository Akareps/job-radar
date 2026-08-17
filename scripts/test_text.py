"""Regression tests for the parsing that scoring depends on.

Run: python scripts/test_text.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from jobradar.text import (  # noqa: E402
    classify_seniority, detect_language, parse_min_years, requires_language,
)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")


# Location classification now lives in geo.py / location.py -> test_geo.py

# --- seniority ---------------------------------------------------------------
check("senior title", classify_seniority("Senior Data Scientist"), "senior")
check("sr abbreviated", classify_seniority("Sr. ML Engineer"), "senior")
check("lead is senior", classify_seniority("Lead Quantitative Researcher"), "senior")
check("head of is senior", classify_seniority("Head of Data Science"), "senior")
check("junior title", classify_seniority("Junior Machine Learning Engineer"), "junior")
check("graduate title", classify_seniority("Graduate Quantitative Trader"), "junior")
check("new grad", classify_seniority("New Grad Software Engineer"), "junior")
check("intern", classify_seniority("Quantitative Research Internship"), "intern")
check("senior beats body",
      classify_seniority("Senior Engineer", "ideal for a graduate"), "senior")
check("years 5 -> senior",
      classify_seniority("Data Scientist", "at least 5 years of experience"), "senior")
check("years 2 -> mid",
      classify_seniority("Data Scientist", "2+ years of experience"), "mid")
check("no signal", classify_seniority("Data Scientist", "join our team"), "unknown")
check("leader is senior", classify_seniority("Clinical Program Leader"), "senior")
check("team leader is senior", classify_seniority("Data Team Leader"), "senior")
# These scored 100+ as seniority=unknown before.
check("experienced is senior",
      classify_seniority("Experienced Quantitative Strategist"), "senior")
check("advanced is senior", classify_seniority("Data Scientist - Advanced Analytics"), "senior")
check("expert is senior", classify_seniority("Expert Data Engineer"), "senior")
check("associate director is senior",
      classify_seniority("Associate Director, Biostatistics"), "senior")
# Working students are a term-time side job, not a post-MSc role.
check("werkstudent", classify_seniority("Werkstudent:in (Gen)AI Engineer"), "working_student")
check("working student", classify_seniority("Trader Assistant (Working Student)"), "working_student")
check("working student beats senior marker",
      classify_seniority("Working Student, Senior Data Team"), "working_student")
check("graduate still junior", classify_seniority("Graduate Quantitative Trader"), "junior")

# --- years -------------------------------------------------------------------
check("plus form", parse_min_years("You have 3+ years of experience"), 3)
check("at least", parse_min_years("at least 5 years of relevant experience"), 5)
check("range", parse_min_years("5-7 years of professional experience"), 5)
check("german", parse_min_years("mindestens 4 Jahre Berufserfahrung"), 4)
check("absent", parse_min_years("no requirement stated"), None)
check("picks minimum",
      parse_min_years("2 years of experience; 8 years experience preferred"), 2)

# --- language ----------------------------------------------------------------
check("german posting", detect_language(
    "Wir suchen eine Mitarbeiterin",
    "Sie sind für die Aufgaben und Anforderungen in unserem Bereich zuständig "
    "und werden mit unserem Team sowie den Kunden arbeiten"), "de")
check("english posting", detect_language(
    "We are looking for a data scientist to join our team and build models"), "en")
check("german required", requires_language("Fliessend Deutsch erforderlich", "de"), True)
check("german c1", requires_language("German (C1) and English", "de"), True)
check("fluent german en", requires_language("fluent in German is required", "de"), True)
check("german nice-to-have not flagged",
      requires_language("English required, German a plus", "de"), False)
check("french required", requires_language("Maitrise du francais exigee", "fr"), True)
check("dutch required", requires_language("Fluent Dutch is required", "nl"), True)
check("unrelated language not flagged", requires_language("fluent English", "de"), False)

if failures:
    print(f"FAILED {len(failures)} check(s):\n")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All text/location/seniority checks passed.")
