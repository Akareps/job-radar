"""Ranking regression tests: role families, company tiers, hard caps.

Run: python scripts/test_scoring.py
"""

import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from jobradar.models import Job       # noqa: E402
from jobradar.scoring import Scorer   # noqa: E402

root = Path(__file__).resolve().parent.parent
profile = yaml.safe_load((root / "config" / "profile.example.yaml").read_text(encoding="utf-8"))
# Keep employer matching regression coverage without baking anyone's target list
# into the public example profile.
profile["company_tiers"] = {"A": ["Acme"], "B": ["Beta Labs"]}
profile["pay_bands"]["companies"] = {1: ["Acme"], 3: ["Beta Labs"]}
profile_path = Path(tempfile.mkdtemp()) / "profile.yaml"
profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
sc = Scorer(profile_path)
failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")


def score(title, company="Acme", location="Berlin, Germany", desc="", posted=None):
    j = Job(source="ats:greenhouse", source_id="x", title=title, company=company,
            url="u", location=location, description=desc, posted_at=posted)
    return sc.score(j)


# --- role family: specific families must beat the generic quant catch-all ----
for title, want in [
    ("Quant Developer (C++)", "quant_dev"),
    ("Graduate Quant Developer", "quant_dev"),
    ("Quant Devops", "quant_dev"),
    ("Quant Engineer", "quant_dev"),
    ("Quantitative Developer - Python", "quant_dev"),
    ("Quant Developer DeFi - Digital Assets", "quant_dev"),
    ("Quantitative Researcher", "quant_research"),
    ("Quant Researcher - ML Alpha", "quant_research"),
    ("Quantitative Analyst", "quant_research"),
    ("Quantitative Equity Researcher", "quant_research"),
    ("Quantitative Credit Strategist", "quant_research"),
    ("Quantitative AI Strategist", "quant_research"),
    ("Junior Quantitative Portfolio Manager", "quant_research"),
    ("Quantitative Portfolio Analyst", "quant_research"),
    ("Quantitative Risk Manager", "quant_research"),
    # An allow-list of domain words dropped all of these; the middle is open.
    ("Quantitative Equities Researcher", "quant_research"),
    ("Quantitative Macro Strategist", "quant_research"),
    ("Quantitative FX Analyst", "quant_research"),
    ("Quantitative Volatility Researcher", "quant_research"),
    ("Quantitative Multi-Asset Analyst", "quant_research"),
    ("Quantitative Commodities Researcher", "quant_research"),
    ("Quantitative Market Risk Analyst", "quant_research"),
    ("Quantitative Prediction Markets Research Summer Analyst", "quant_research"),
    ("Quantitative Risk Developer", "quant_dev"),
    ("Quantitative Trader", "quant_trading"),
    ("Graduate Trader", "quant_trading"),
    ("Machine Learning Engineer", "ml_engineer"),
    ("Data Scientist", "data_scientist"),
    ("Software Engineer", "target_roles"),
    ("Product Manager", "target_roles"),
    # Anchoring regressions: unanchored bio terms matched inside other words.
    ("Data Scientist (Economics)", "data_scientist"),
    ("Economics Research Analyst", "other"),
    ("Verantwortliche/-r Einsatz Führungssysteme Luftwaffe", "other"),
    ("Genetic Algorithm Optimisation Engineer", "other"),
    ("Ergonomics Specialist", "other"),
    # "Quantitative" also describes non-finance research and operational work.
    # These must not inherit the 100-point quant-research base.
    ("Quantitative Marketing Research Manager", "other"),
    ("Quantitative User Researcher", "other"),
    ("Quantitative UX Researcher", "other"),
    ("Quantitative Clinical Research Analyst", "other"),
    ("Quantitative Survey Researcher", "other"),
    ("Quantitative Customer Insights Analyst", "other"),
    ("Quantitative Campaign Manager", "other"),
    ("Quantitative Project Manager", "other"),
    # "Market research" is the one non-finance sense built from finance words.
    ("Quantitative Market Research Analyst", "other"),
    ("Quantitative Marketing Specialist", "other"),
]:
    check(f"role: {title!r}", sc.classify_role(title), want)

# The description fallback only fires for programme-shaped titles, and must not
# drag them into an unrelated family via a stray word in the body.

# --- company tier: whole tokens, not substrings ------------------------------
check("Acme is tier A", sc.company_tier("Acme Inc."), "A")
check("Acmeology is not Acme", sc.company_tier("Acmeology"), "C")
check("Beta Labs is tier B", sc.company_tier("Beta Labs GmbH"), "B")
check("Alphabetalabs is not Beta Labs", sc.company_tier("Alphabetalabs"), "C")

# --- accented Swiss names ----------------------------------------------------
# Dropping non-ASCII turned "Zürcher" into "z rcher", so these silently fell to
# tier C and the bottom pay band.

# --- pay bands ---------------------------------------------------------------
check("Acme band 1", sc.pay_band("Acme Inc."), 1)
check("Beta Labs band 3", sc.pay_band("Beta Labs"), 3)
check("unknown startup band 4", sc.pay_band("Tiny Startup GmbH"), 4)
check("Acmeology does not inherit Acme band", sc.pay_band("Acmeology"), 4)

# --- hard caps ---------------------------------------------------------------
senior = score("Senior Quant Researcher - Fixed Income", "Example Quant Firm")
check("senior capped", senior.score <= 45, True)
check("senior recorded", senior.score_breakdown.get("capped_by"), ["senior"])

phd = score("Quantitative Researcher – PhD & Postdoc Opportunities",
            "Example Research Firm",
            desc="A PhD degree is required for this position.")
check("phd-required capped", phd.score <= 48, True)

ws = score("Werkstudent:in (Gen)AI Engineer", "Example Employer")
check("working student capped", ws.score <= 35, True)

experienced = score("Experienced Quantitative Strategist", "Example Quant Firm")
check("experienced is capped as senior", experienced.score <= 45, True)

# A genuinely reachable role must outrank all of the above.
junior = score("Graduate Quantitative Researcher", "Example Trading Firm", posted=None)
check("junior beats senior", junior.score > senior.score, True)
check("junior beats phd", junior.score > phd.score, True)
check("junior beats working student", junior.score > ws.score, True)
check("junior beats experienced", junior.score > experienced.score, True)

# Uncapped roles must be untouched.
plain = score("Quantitative Researcher", "Example Trading Firm")
check("uncapped role has no cap recorded", "capped_by" in plain.score_breakdown, False)

# Rescoring an Arbeitnow row stored before country normalization must still
# interpret its bare DE location as Germany, not Delaware or unknown.
us_profile = yaml.safe_load(yaml.safe_dump(profile))
us_profile["location"]["home"] = "Durham, North Carolina, USA"
us_profile["location"]["also_good"] = [
    {"place": "New York, USA", "tier": "hub", "points": 16, "within_km": 40},
    {"place": "San Francisco, USA", "tier": "hub", "points": 16, "within_km": 40},
    {"place": "Seattle, USA", "tier": "hub", "points": 16, "within_km": 40},
]
us_profile["location"]["name_hints"] = {
    "local": ["durham"],
    "hub": ["new york", "san francisco", "seattle"],
}
us_profile_path = profile_path.with_name("us-profile.yaml")
us_profile_path.write_text(yaml.safe_dump(us_profile, sort_keys=False), encoding="utf-8")
us_scorer = Scorer(us_profile_path)
legacy_de = Job(
    source="arbeitnow", source_id="legacy-de",
    title="Software Engineer - Stuttgart, Germany", company="Example",
    url="u", location="DE",
)
us_scorer.score(legacy_de)
check("legacy Arbeitnow DE is elsewhere",
      legacy_de.score_breakdown["location_tier"], "elsewhere")
check("legacy Arbeitnow DE retains Germany", legacy_de.country, "DE")

# A low role weight can keep the balanced score below a limit while another
# ranking mode lifts it above that limit. Every applicable cap must survive.
from copy import copy, deepcopy  # noqa: E402
from jobradar.ranking import MODES, effective_score  # noqa: E402

cap_scorer = copy(sc)
cap_scorer.families = deepcopy(sc.families)
for spec in cap_scorer.families.values():
    spec["weight"] = 5

for title, desc, reason in [
    ("Senior Software Engineer", "", "senior"),
    ("Software Engineer", "A PhD degree is required.", "phd_required"),
    ("Werkstudent Software Engineer", "", "working_student"),
    ("PhD Researcher in Machine Learning", "", "phd_position"),
    ("Software Engineer", "Security clearance required.", "clearance"),
]:
    job = cap_scorer.score(Job(
        source="ats:greenhouse", source_id=reason, title=title,
        company='Acme', url="https://example.com/job",
        location='Berlin, Germany', description=desc,
    ))
    ceiling = cap_scorer._caps[reason]
    check(f"{reason}: fixture starts below cap", job.score < ceiling, True)
    check(f"{reason}: limit retained", job.score_breakdown.get("cap_ceiling"), ceiling)
    check(f"{reason}: explanation retained",
          reason in job.score_breakdown.get("capped_by", []), True)
    without_cap = {key: value for key, value in job.score_breakdown.items()
                   if key != "cap_ceiling"}
    check(f"{reason}: pay would cross cap",
          effective_score(without_cap, MODES["pay"]) > ceiling, True)
    for mode in MODES.values():
        check(f"{reason}: cap holds in {mode.key}",
              effective_score(job.score_breakdown, mode) <= ceiling, True)
        check(f"{reason}: cap holds with overrides in {mode.key}",
              effective_score(job.score_breakdown, mode,
                              {"location": 100, "company": 100}) <= ceiling, True)

# Equality is another boundary: the cap still applies after reweighting.
equal_scorer = copy(sc)
equal_scorer._caps = {"senior": 10000}
equal_job = Job(source="ats:greenhouse", source_id="equal-cap",
                title="Senior Software Engineer", company='Acme',
                url="https://example.com/equal", location='Berlin, Germany')
equal_scorer.score(equal_job)
equal_scorer._caps = {"senior": equal_job.score_breakdown["uncapped_score"]}
equal_scorer.score(equal_job)
check("score equal to limit keeps ceiling", equal_job.score_breakdown.get("cap_ceiling"),
      equal_scorer._caps["senior"])
check("score equal to limit cannot rise in pay mode",
      effective_score(equal_job.score_breakdown, MODES["pay"]) <= equal_job.score, True)

if failures:
    print(f"FAILED {len(failures)} check(s):\n")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All role/company/pay/cap checks passed.")
