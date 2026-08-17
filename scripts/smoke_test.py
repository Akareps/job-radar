"""Smoke test: hit one real board per adapter and print scored results.

Run after changing an adapter:  python scripts/smoke_test.py

Uses public boards that exist regardless of your configuration, so it verifies
the adapters themselves rather than your company list.
"""

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from jobradar.http import Http                                   # noqa: E402
from jobradar.scoring import Scorer                              # noqa: E402
from jobradar.sources.ats import (                               # noqa: E402
    Ashby, Greenhouse, Lever, SmartRecruiters, Workday,
)
from jobradar.sources.boards import Arbeitnow                    # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

profile = ROOT / "config" / "profile.yaml"
if not profile.exists():
    profile = ROOT / "config" / "profile.example.yaml"
    print(f"No profile.yaml yet - scoring against {profile.name}.\n")

http = Http()
scorer = Scorer(profile, city_cache=ROOT / "data" / "cities.tsv.gz")
print(scorer.location.describe(), "\n")


def show(label, source, limit=4):
    jobs, err = source.safe_fetch(http)
    if err:
        print(f"=== {label}: ERROR {err[:120]}")
        return
    for job in jobs:
        scorer.score(job)
    jobs.sort(key=lambda j: -j.score)
    print(f"=== {label}: {len(jobs)} jobs ===")
    for job in jobs[:limit]:
        tier = job.score_breakdown.get("location_tier", "?")
        print(f"  {job.score:6.1f} | {job.title[:46]:46} | "
              f"{(job.city or '-')[:16]:16} | {job.role_family}/{job.seniority} | {tier}")
    print()


show("Greenhouse / Anthropic", Greenhouse("Anthropic", "anthropic"))
show("Lever / ANYbotics", Lever("ANYbotics", "anybotics"))
show("Ashby / OpenAI", Ashby("OpenAI", "openai"))
show("SmartRecruiters / CERN", SmartRecruiters("CERN", "cern"))
show("Workday / Roche", Workday("Roche", "roche/roche-ext", host="roche.wd3", max_pages=2))
show("Arbeitnow", Arbeitnow(pages=1))
