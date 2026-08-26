"""Tests for storage: upsert, triage preservation, dedup, cross-DB merge.

Run: python scripts/test_db.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from jobradar.db import Store          # noqa: E402
from jobradar.models import Job        # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")


def job(source, sid, title, company, city="Zurich", score=100.0):
    j = Job(source=source, source_id=sid, title=title, company=company,
            url=f"https://example.com/{sid}", location=city)
    j.score = score
    j.city = city
    return j


tmpdir = Path(tempfile.mkdtemp())

# --- upsert preserves triage state -------------------------------------------
with Store(tmpdir / "a.db") as s:
    n_new, n_upd = s.upsert_jobs([job("ats:greenhouse", "1", "Quant Researcher", "Zeta Markets")])
    check("first insert", (n_new, n_upd), (1, 0))

    jid = job("ats:greenhouse", "1", "Quant Researcher", "Zeta Markets").id
    s.set_status(jid, "applied")

    # Same posting reappears in the next scan with a refreshed score.
    n_new, n_upd = s.upsert_jobs([job("ats:greenhouse", "1", "Quant Researcher", "Zeta Markets", score=140.0)])
    check("second scan updates", (n_new, n_upd), (0, 1))
    row = s.get(jid)
    check("status survives rescan", row["status"], "applied")
    check("score refreshed", row["score"], 140.0)

# --- cross-source dedup ------------------------------------------------------
with Store(tmpdir / "b.db") as s:
    s.upsert_jobs([
        job("linkedin", "L1", "Quantitative Researcher", "Zeta Markets", score=120),
        job("jobs.ch", "J1", "Quantitative Researcher (m/f/d)", "Zeta Markets AG", score=118),
        job("ats:greenhouse", "G1", "Quantitative Researcher", "Zeta Markets", score=115),
        job("linkedin", "L2", "Data Scientist", "Roche", score=90),
    ])
    s.mark_duplicates()
    primaries = s.query(limit=50)
    check("dedup collapses the Zeta Markets trio", len(primaries), 2)
    # The ATS row wins even though it scores lowest - it has the real apply link.
    zeta = [r for r in primaries if "Zeta Markets" in r["company"]][0]
    check("ATS row wins the tie", zeta["source"], "ats:greenhouse")
    check("duplicates still stored", s.counts()["duplicates"], 2)

# --- merge between two databases ---------------------------------------------
with Store(tmpdir / "local.db") as local:
    local.upsert_jobs([
        job("linkedin", "L1", "Quant Researcher", "IMC", score=130),
        job("linkedin", "L2", "ML Engineer", "Roche", score=110),
    ])
    local.set_status(job("linkedin", "L1", "Quant Researcher", "IMC").id, "rejected")

with Store(tmpdir / "remote.db") as remote:
    remote.upsert_jobs([
        job("linkedin", "L1", "Quant Researcher", "IMC", score=999),   # same posting
        job("jobs.ch", "R9", "Quant Trader", "Flow Traders", score=150),  # new
    ])

with Store(tmpdir / "local.db") as local:
    inserted, refreshed, touched = local.merge_from(tmpdir / "remote.db")
    check("merge inserts only the unseen posting", inserted, 1)
    check("total after merge", local.counts()["total"], 3)
    rejected = local.get(job("linkedin", "L1", "Quant Researcher", "IMC").id)
    check("local rejection survives merge", rejected["status"], "rejected")
    check("rejected row not overwritten by remote score", rejected["score"], 130.0)
    check("merged row present",
          local.get(job("jobs.ch", "R9", "Quant Trader", "Flow Traders").id) is not None, True)
    check("merge reports the ids it touched", sorted(touched) != [], True)

# --- merge must NOT import the remote's scoring ------------------------------
# Scoring is derived from the local profile.yaml. A cloud database scored under
# an older profile once overwrote 10,997 freshly corrected rows.
with Store(tmpdir / "l2.db") as local:
    local.upsert_jobs([job("linkedin", "S1", "Senior Quant Researcher", "IMC", score=45.0)])
    local.conn.execute("UPDATE jobs SET role_family='quant_research', seniority='senior'")
    local.conn.commit()
with Store(tmpdir / "r2.db") as remote:
    remote.upsert_jobs([job("linkedin", "S1", "Senior Quant Researcher", "IMC", score=177.0)])
    remote.conn.execute("UPDATE jobs SET role_family='WRONG', seniority='unknown'")
    remote.conn.commit()
with Store(tmpdir / "l2.db") as local:
    local.merge_from(tmpdir / "r2.db")
    row = local.get(job("linkedin", "S1", "Senior Quant Researcher", "Alpha Quant").id)
    check("remote score is NOT imported", row["score"], 45.0)
    check("remote role_family is NOT imported", row["role_family"], "quant_research")
    check("remote seniority is NOT imported", row["seniority"], "senior")

# Raw board facts, by contrast, must come through.
with Store(tmpdir / "l3.db") as local:
    local.upsert_jobs([job("linkedin", "T1", "Old Title", "Alpha Quant")])
with Store(tmpdir / "r3.db") as remote:
    remote.upsert_jobs([job("linkedin", "T1", "Corrected Title", "Alpha Quant Trading")])
with Store(tmpdir / "l3.db") as local:
    local.merge_from(tmpdir / "r3.db")
    row = local.get(job("linkedin", "T1", "Old Title", "Alpha Quant").id)
    check("remote title IS imported", row["title"], "Corrected Title")
    check("remote company IS imported", row["company"], "Alpha Quant Trading")

# --- pruning: bounds growth without touching your decisions -----------------
with Store(tmpdir / "prune.db") as s:
    s.upsert_jobs([
        job("linkedin", "P1", "Junk Role", "Nowhere Inc", score=5),    # old + junk
        job("linkedin", "P2", "Junk Role 2", "Nowhere Inc", score=5),  # recent + junk
        job("linkedin", "P3", "Great Quant Role", "Alpha Quant", score=150),  # old + valuable
        job("linkedin", "P4", "Applied Junk", "Nowhere Inc", score=5), # old + junk BUT applied
        job("linkedin", "P5", "Ancient Good", "Alpha Quant", score=150),    # very old + valuable
    ])
    s.set_status(job("linkedin", "P4", "Applied Junk", "Nowhere Inc").id, "applied")
    old = "datetime('now','-40 days')"
    ancient = "datetime('now','-400 days')"
    for sid, expr in [("P1", old), ("P3", old), ("P4", old), ("P5", ancient)]:
        jid = job("linkedin", sid, "x", "y").id
        s.conn.execute(f"UPDATE jobs SET first_seen = {expr} WHERE id = ?", (jid,))
    s.conn.commit()

    plan = s.prune(max_age_days=21, below_score=45, hard_max_age_days=180, dry_run=True)
    check("dry run changes nothing", plan["removed"], 0)
    check("dry run counts P1 and P5", plan["would_remove"], 2)

    stats = s.prune(max_age_days=21, below_score=45, hard_max_age_days=180)
    check("pruned old junk and ancient rows", stats["removed"], 2)
    alive = {r["source_id"] for r in s.conn.execute("SELECT source_id FROM jobs")}
    check("old junk removed", "P1" in alive, False)
    check("recent junk kept", "P2" in alive, True)
    check("old but valuable kept", "P3" in alive, True)
    check("APPLIED row kept despite being old junk", "P4" in alive, True)
    check("ancient row removed even though valuable", "P5" in alive, False)

# --- description cap ---------------------------------------------------------
from jobradar.models import MAX_DESCRIPTION_CHARS  # noqa: E402

huge = Job(source="linkedin", source_id="D1", title="T", company="C", url="u",
           description="x" * 50_000)
check("to_row truncates for storage",
      len(huge.to_row()["description"]) <= MAX_DESCRIPTION_CHARS + 20, True)
check("in-memory description untouched, so scoring sees it all",
      len(huge.description), 50_000)

with Store(tmpdir / "trunc.db") as s:
    s.conn.execute(
        "INSERT INTO jobs (id, dedupe_key, source, source_id, title, company, "
        "description, first_seen, last_seen) VALUES ('x','k','s','1','t','c',?, "
        "datetime('now'), datetime('now'))", ("y" * 40_000,))
    s.conn.commit()
    check("legacy oversized row shortened", s.truncate_descriptions(8000), 1)
    length = s.conn.execute("SELECT LENGTH(description) L FROM jobs").fetchone()["L"]
    check("shortened to the cap", length <= 8020, True)
    check("vacuum returns a byte delta", isinstance(s.vacuum(), int), True)

# --- gzip round-trip, which is how the cloud database now travels ------------
import gzip  # noqa: E402

with Store(tmpdir / "gz_src.db") as s:
    s.upsert_jobs([job("linkedin", "G1", "Quant Researcher", "IMC", score=150)])
blob = (tmpdir / "gz_src.db").read_bytes()
packed = gzip.compress(blob, 9)
check("gzip actually shrinks the db", len(packed) < len(blob), True)
(tmpdir / "gz_dst.db").write_bytes(gzip.decompress(packed))
with Store(tmpdir / "target.db") as s:
    inserted, _, _ = s.merge_from(tmpdir / "gz_dst.db")
    check("decompressed db merges cleanly", inserted, 1)

# --- stale scan rows get closed ---------------------------------------------
with Store(tmpdir / "scans.db") as s:
    sid = s.start_scan()
    s.conn.execute("UPDATE scans SET started_at = datetime('now','-5 hours') WHERE id = ?", (sid,))
    s.conn.commit()
    check("stale scan closed", s.close_stale_scans(90), 1)
    check("no open scans left",
          s.conn.execute("SELECT COUNT(*) c FROM scans WHERE finished_at IS NULL").fetchone()["c"], 0)
    fresh = s.start_scan()
    check("fresh scan untouched", s.close_stale_scans(90), 0)

# --- stable identity ---------------------------------------------------------
a = job("linkedin", "X", "Quant", "Alpha Quant")
b = job("linkedin", "X", "Quant Researcher", "Alpha Quant Trading")
check("id keyed on source only", a.id, b.id)
c = job("jobs.ch", "X", "Quant", "Alpha Quant")
check("different source, different id", a.id == c.id, False)
check("dedupe key ignores legal suffix and m/f/d",
      job("a", "1", "Data Scientist (m/f/d)", "Gamma Labs AG").dedupe_key,
      job("b", "2", "Data Scientist", "Gamma Labs").dedupe_key)
# The same posting reaches us named differently by each source.
for left, right in [
    ("Alpha Quant", "Alpha Quant Trading"),
    ("Beta Capital", "Beta"),
    ("Gamma Labs LLC - Careers", "Gamma Labs"),
    ("Delta Research Capital", "Delta Research"),
    ("Epsilon Holding B.V.", "Epsilon"),
]:
    check(f"dedupe collapses {left!r} / {right!r}",
          job("a", "1", "Graduate Quant Researcher", left).dedupe_key,
          job("b", "2", "Graduate Quant Researcher", right).dedupe_key)
# ...but must not collapse genuinely different employers.
check("distinct employers stay distinct",
      job("a", "1", "Quant Researcher", "Orion Trading").dedupe_key
      != job("b", "2", "Quant Researcher", "Vega Research").dedupe_key, True)

if failures:
    print(f"FAILED {len(failures)} check(s):\n")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All storage/dedup/merge checks passed.")
