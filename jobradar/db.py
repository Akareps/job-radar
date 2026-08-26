"""SQLite store. Holds jobs, your triage state, and per-source health."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .models import Job, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    dedupe_key        TEXT NOT NULL,
    source            TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    title             TEXT NOT NULL,
    company           TEXT NOT NULL,
    location          TEXT,
    city              TEXT,
    country           TEXT,
    url               TEXT,
    apply_url         TEXT,
    description       TEXT,
    posted_at         TEXT,
    employment_type   TEXT,
    language          TEXT,
    remote            INTEGER DEFAULT 0,
    department        TEXT,
    role_family       TEXT,
    seniority         TEXT,
    score             REAL DEFAULT 0,
    score_breakdown   TEXT,
    tags              TEXT,
    required_languages TEXT,
    min_years_experience INTEGER,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'new',
    notes             TEXT DEFAULT '',
    is_primary        INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_jobs_score   ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe  ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_jobs_seen    ON jobs(first_seen DESC);

CREATE TABLE IF NOT EXISTS scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    n_fetched    INTEGER DEFAULT 0,
    n_new        INTEGER DEFAULT 0,
    n_updated    INTEGER DEFAULT 0,
    duration_s   REAL,
    errors       TEXT
);

CREATE TABLE IF NOT EXISTS source_health (
    source        TEXT PRIMARY KEY,
    last_run      TEXT,
    last_ok       TEXT,
    last_count    INTEGER DEFAULT 0,
    last_error    TEXT,
    fail_streak   INTEGER DEFAULT 0,
    total_jobs    INTEGER DEFAULT 0
);
"""

# Raw facts as published by the board. Safe to copy between databases.
_SOURCE_FIELDS = (
    "title", "company", "location", "url", "apply_url", "description",
    "posted_at", "employment_type", "remote", "department",
    "required_languages", "min_years_experience",
)

# Everything the scorer computes from profile.yaml. These are a *local*
# derivation, not a property of the posting: two machines running different
# profile versions produce different values for the same job. They must never
# be imported from another database - doing so let a cloud scan taken before a
# scoring fix overwrite 10,997 freshly corrected rows and put a misclassified
# "Quant Developer (C++)" back at the top of the list.
_DERIVED_FIELDS = (
    "city", "country", "language", "role_family", "seniority",
    "score", "score_breakdown", "tags", "dedupe_key",
)

# Columns the local scanner may refresh. Everything outside this list
# (status, notes, first_seen) is yours and never overwritten.
_REFRESHABLE = _SOURCE_FIELDS + _DERIVED_FIELDS

VALID_STATUS = {"new", "seen", "shortlist", "applied", "rejected", "hidden"}


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #

    def upsert_jobs(self, jobs: Iterable[Job]) -> tuple[int, int]:
        """-> (n_new, n_updated). Preserves triage state on existing rows."""
        now = utcnow()
        n_new = n_updated = 0
        cur = self.conn.cursor()

        for job in jobs:
            row = job.to_row()
            row["score_breakdown"] = json.dumps(job.score_breakdown, ensure_ascii=False)
            row["tags"] = json.dumps(job.tags, ensure_ascii=False)
            row["required_languages"] = json.dumps(job.required_languages, ensure_ascii=False)
            row["remote"] = int(bool(job.remote))

            existing = cur.execute("SELECT id FROM jobs WHERE id = ?", (row["id"],)).fetchone()

            if existing:
                sets = ", ".join(f"{c} = :{c}" for c in _REFRESHABLE)
                cur.execute(
                    f"UPDATE jobs SET {sets}, last_seen = :last_seen WHERE id = :id",
                    {**row, "last_seen": now},
                )
                n_updated += 1
            else:
                cols = list(_REFRESHABLE) + ["id", "source", "source_id", "first_seen", "last_seen"]
                placeholders = ", ".join(f":{c}" for c in cols)
                cur.execute(
                    f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({placeholders})",
                    {**row, "first_seen": now, "last_seen": now},
                )
                n_new += 1

        self.conn.commit()
        return n_new, n_updated

    def mark_duplicates(self) -> int:
        """Keep the highest-scoring row per dedupe_key as primary, demote the rest.

        Direct-from-company postings win ties: they carry the real apply link.
        """
        cur = self.conn.cursor()
        cur.execute("UPDATE jobs SET is_primary = 1")
        cur.execute(
            """
            UPDATE jobs SET is_primary = 0
            WHERE id NOT IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY dedupe_key
                        ORDER BY (source LIKE 'ats:%') DESC, score DESC, first_seen ASC
                    ) AS rn
                    FROM jobs
                    WHERE status != 'hidden'
                ) WHERE rn = 1
            )
            AND status != 'hidden'
            """
        )
        demoted = cur.rowcount
        self.conn.commit()
        return demoted

    def set_status(self, job_id: str, status: str) -> bool:
        if status not in VALID_STATUS:
            raise ValueError(f"invalid status {status!r}")
        cur = self.conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
        self.conn.commit()
        return cur.rowcount > 0

    def set_notes(self, job_id: str, notes: str) -> bool:
        cur = self.conn.execute("UPDATE jobs SET notes = ? WHERE id = ?", (notes, job_id))
        self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------ #
    # Scans and health
    # ------------------------------------------------------------------ #

    def start_scan(self) -> int:
        cur = self.conn.execute("INSERT INTO scans (started_at) VALUES (?)", (utcnow(),))
        self.conn.commit()
        return cur.lastrowid

    def finish_scan(self, scan_id: int, *, fetched: int, new: int, updated: int,
                    duration: float, errors: list[str]) -> None:
        self.conn.execute(
            "UPDATE scans SET finished_at=?, n_fetched=?, n_new=?, n_updated=?, "
            "duration_s=?, errors=? WHERE id=?",
            (utcnow(), fetched, new, updated, duration,
             json.dumps(errors, ensure_ascii=False), scan_id),
        )
        self.conn.commit()

    #: Statuses that represent your own decisions. Pruning never touches these,
    #: however old or low-scoring the row is - they are your application record.
    PROTECTED_STATUSES = ("shortlist", "applied", "rejected")

    def prune(self, *, max_age_days: int = 21, below_score: float | None = 45.0,
              hard_max_age_days: int | None = 180,
              dry_run: bool = False) -> dict[str, int]:
        """Drop stale, untriaged, uninteresting rows. -> counts.

        This keeps long-running databases below hosting and artifact size limits.

        Two rules, both of which skip anything you triaged:

        * low-value: scores under `below_score` and first seen more than
          `max_age_days` ago. 85% of rows score under 20 - global postings
          located outside every band in your profile, which are never
          shown.
        * ancient: first seen more than `hard_max_age_days` ago, whatever
          it scores.

        Deleting is safe because the boards are the source of truth, not this
        cache: anything still open is re-fetched on the next scan.
        """
        protected = ", ".join("?" * len(self.PROTECTED_STATUSES))
        rules: list[str] = []
        params: list[Any] = []

        if below_score is not None:
            rules.append("(score < ? AND julianday('now') - julianday(first_seen) > ?)")
            params += [below_score, max_age_days]
        else:
            rules.append("(julianday('now') - julianday(first_seen) > ?)")
            params.append(max_age_days)

        if hard_max_age_days is not None:
            rules.append("(julianday('now') - julianday(first_seen) > ?)")
            params.append(hard_max_age_days)

        clause = f"status NOT IN ({protected}) AND ({' OR '.join(rules)})"
        params = [*self.PROTECTED_STATUSES, *params]

        doomed = self.conn.execute(
            f"SELECT COUNT(*) n FROM jobs WHERE {clause}", params
        ).fetchone()["n"]
        before = self.conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]

        if dry_run or not doomed:
            return {"removed": 0, "would_remove": doomed, "remaining": before}

        self.conn.execute(f"DELETE FROM jobs WHERE {clause}", params)
        self.conn.commit()
        remaining = self.conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
        self.mark_duplicates()
        return {"removed": before - remaining, "would_remove": doomed,
                "remaining": remaining}

    def vacuum(self) -> int:
        """Rebuild the file to reclaim freed pages. -> bytes saved.

        DELETE only marks pages reusable; the file itself never shrinks
        without this, which is why 'just delete old rows' does not fix a
        size-limit failure on its own.
        """
        before = self.path.stat().st_size if self.path.exists() else 0
        # VACUUM cannot run inside a transaction.
        self.conn.isolation_level = None
        try:
            self.conn.execute("VACUUM")
        finally:
            self.conn.isolation_level = ""
        after = self.path.stat().st_size if self.path.exists() else 0
        return before - after

    #: Appended to a shortened description. Also the marker that stops a row
    #: being rewritten on every pass: a capped row is max_chars + len(MARKER)
    #: long, which still exceeds max_chars, so a naive length test would rewrite
    #: already-truncated rows on every sync and report them as fresh work.
    TRUNCATION_MARKER = "\n[truncated]"

    def truncate_descriptions(self, max_chars: int) -> int:
        """Shorten oversized stored descriptions. -> rows actually changed.

        Brings a database written before the cap existed - or one merged from a
        remote that predates it - into line.
        """
        cur = self.conn.execute(
            "UPDATE jobs SET description = substr(description, 1, ?) || ? "
            "WHERE LENGTH(description) > ? AND description NOT LIKE ?",
            (max_chars, self.TRUNCATION_MARKER, max_chars,
             f"%{self.TRUNCATION_MARKER}"),
        )
        self.conn.commit()
        return cur.rowcount

    def close_stale_scans(self, older_than_minutes: int = 90) -> int:
        """Mark abandoned scan rows as failed.

        A scan killed mid-flight never reaches finish_scan, so its row sits
        with finished_at NULL forever and `jobradar health` shows a run that
        looks permanently in progress. Anything older than the cutoff is
        recorded as interrupted so the history stays honest.
        """
        cur = self.conn.execute(
            "UPDATE scans SET finished_at = ?, errors = ? "
            "WHERE finished_at IS NULL "
            "AND (julianday('now') - julianday(started_at)) * 1440 > ?",
            (utcnow(), json.dumps(["interrupted: process exited before finishing"]),
             older_than_minutes),
        )
        self.conn.commit()
        return cur.rowcount

    def fail_scan(self, scan_id: int, reason: str) -> None:
        self.conn.execute(
            "UPDATE scans SET finished_at = ?, errors = ? WHERE id = ?",
            (utcnow(), json.dumps([reason[:500]]), scan_id),
        )
        self.conn.commit()

    def record_source(self, source: str, count: int, error: str | None) -> None:
        now = utcnow()
        row = self.conn.execute(
            "SELECT fail_streak, total_jobs FROM source_health WHERE source = ?", (source,)
        ).fetchone()
        streak = (row["fail_streak"] if row else 0)
        total = (row["total_jobs"] if row else 0)

        if error:
            streak += 1
            self.conn.execute(
                "INSERT INTO source_health (source, last_run, last_count, last_error, fail_streak, total_jobs) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
                "last_run=excluded.last_run, last_count=excluded.last_count, "
                "last_error=excluded.last_error, fail_streak=excluded.fail_streak",
                (source, now, count, error[:500], streak, total),
            )
        else:
            self.conn.execute(
                "INSERT INTO source_health (source, last_run, last_ok, last_count, last_error, fail_streak, total_jobs) "
                "VALUES (?,?,?,?,NULL,0,?) ON CONFLICT(source) DO UPDATE SET "
                "last_run=excluded.last_run, last_ok=excluded.last_ok, "
                "last_count=excluded.last_count, last_error=NULL, fail_streak=0, "
                "total_jobs=excluded.total_jobs",
                (source, now, now, count, total + count),
            )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #

    @staticmethod
    def _filters(
        *,
        status: str | None = None,
        role_family: str | None = None,
        tier: str | None = None,
        source: str | None = None,
        min_score: float | None = None,
        max_age_days: int | None = None,
        search: str | None = None,
        include_duplicates: bool = False,
        include_hidden: bool = False,
    ) -> tuple[str, list[Any]]:
        """Shared WHERE clause so query() and count_matching() cannot drift."""
        where: list[str] = []
        params: list[Any] = []

        if not include_duplicates:
            where.append("is_primary = 1")
        if not include_hidden and status != "hidden":
            where.append("status != 'hidden'")
        if status and status != "all":
            where.append("status = ?")
            params.append(status)
        if role_family and role_family != "all":
            where.append("role_family = ?")
            params.append(role_family)
        if tier and tier != "all":
            where.append("json_extract(score_breakdown, '$.location_tier') = ?")
            params.append(tier)
        if source and source != "all":
            where.append("source = ?")
            params.append(source)
        if min_score is not None:
            where.append("score >= ?")
            params.append(min_score)
        if max_age_days is not None:
            where.append(
                "(posted_at IS NOT NULL AND julianday('now') - julianday(posted_at) <= ?)"
            )
            params.append(max_age_days)
        if search:
            where.append("(title LIKE ? OR company LIKE ? OR description LIKE ?)")
            like = f"%{search}%"
            params += [like, like, like]

        return (f"WHERE {' AND '.join(where)}" if where else ""), params

    def query(self, *, limit: int = 300, offset: int = 0, **filters) -> list[sqlite3.Row]:
        clause, params = self._filters(**filters)
        return self.conn.execute(
            f"SELECT * FROM jobs {clause} ORDER BY score DESC, first_seen DESC "
            f"LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

    def count_matching(self, **filters) -> int:
        """How many rows the filters select, ignoring paging."""
        clause, params = self._filters(**filters)
        return self.conn.execute(
            f"SELECT COUNT(*) n FROM jobs {clause}", params
        ).fetchone()["n"]

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) n FROM jobs WHERE is_primary = 1 GROUP BY status"
        ).fetchall()
        out = {r["status"]: r["n"] for r in rows}
        out["total"] = self.conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE is_primary = 1"
        ).fetchone()["n"]
        out["duplicates"] = self.conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE is_primary = 0"
        ).fetchone()["n"]
        return out

    def facets(self) -> dict[str, list[tuple[str, int]]]:
        def group(col: str) -> list[tuple[str, int]]:
            rows = self.conn.execute(
                f"SELECT {col} k, COUNT(*) n FROM jobs "
                f"WHERE is_primary=1 AND status!='hidden' AND {col} IS NOT NULL AND {col} != '' "
                f"GROUP BY {col} ORDER BY n DESC"
            ).fetchall()
            return [(r["k"], r["n"]) for r in rows]

        tiers = self.conn.execute(
            "SELECT json_extract(score_breakdown,'$.location_tier') k, COUNT(*) n FROM jobs "
            "WHERE is_primary=1 AND status!='hidden' GROUP BY k ORDER BY n DESC"
        ).fetchall()
        return {
            "role_family": group("role_family"),
            "source": group("source"),
            "seniority": group("seniority"),
            "tier": [(r["k"], r["n"]) for r in tiers if r["k"]],
        }

    def source_health(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM source_health ORDER BY fail_streak DESC, last_count DESC"
        ).fetchall()

    def recent_scans(self, limit: int = 12) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def get(self, job_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    # ------------------------------------------------------------------ #
    # Merging
    # ------------------------------------------------------------------ #

    def merge_from(self, other_path: str | Path) -> tuple[int, int, list[str]]:
        """Pull rows from another jobradar DB.

        -> (inserted, refreshed, touched_ids)

        Job ids are a hash of source + source key, so the laptop and the
        GitHub Actions runner generate identical ids for the same posting.
        That makes merging a plain upsert. Two rules keep it safe:

        * Your triage state always wins - a job you rejected stays rejected.
        * Only raw board facts cross the boundary. Scoring fields are a local
          derivation of profile.yaml, so the caller must rescore the returned
          ids against *this* machine's profile.
        """
        other = Path(other_path)
        if not other.exists():
            raise FileNotFoundError(other)

        cur = self.conn.cursor()
        cur.execute("ATTACH DATABASE ? AS incoming", (str(other),))
        try:
            columns = [r[1] for r in cur.execute("PRAGMA table_info(jobs)").fetchall()]
            incoming_cols = [r[1] for r in cur.execute("PRAGMA incoming.table_info(jobs)").fetchall()]
            shared = [c for c in columns if c in incoming_cols]

            new_ids = [r[0] for r in cur.execute(
                "SELECT id FROM incoming.jobs WHERE id NOT IN (SELECT id FROM jobs)"
            ).fetchall()]

            # Only rows whose raw content actually differs need rescoring.
            # Treating every shared row as touched meant rescoring ~11k rows on
            # every hourly sync, when a handful genuinely changed.
            source_cols = [c for c in _SOURCE_FIELDS if c in shared]
            differs = " OR ".join(
                f"IFNULL(i.{c}, '') != IFNULL(j.{c}, '')" for c in source_cols
            ) or "0"
            changed_ids = [r[0] for r in cur.execute(
                f"SELECT i.id FROM incoming.jobs i JOIN jobs j ON j.id = i.id "
                f"WHERE j.status = 'new' AND ({differs})"
            ).fetchall()]

            before = cur.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

            # New postings the remote saw and we did not. Their scoring fields
            # come along only as a placeholder; they are rescored immediately.
            col_list = ", ".join(shared)
            cur.execute(
                f"INSERT INTO jobs ({col_list}) SELECT {col_list} FROM incoming.jobs "
                f"WHERE id NOT IN (SELECT id FROM jobs)"
            )
            inserted = cur.rowcount

            # Known postings: refresh raw facts only, never scoring output, and
            # pull first_seen back if the remote spotted it earlier than we did.
            sets = ", ".join(f"{c} = (SELECT i.{c} FROM incoming.jobs i WHERE i.id = jobs.id)"
                             for c in source_cols)
            cur.execute(
                f"UPDATE jobs SET {sets}, "
                f"first_seen = MIN(first_seen, "
                f"  (SELECT i.first_seen FROM incoming.jobs i WHERE i.id = jobs.id)), "
                f"last_seen = MAX(last_seen, "
                f"  (SELECT i.last_seen FROM incoming.jobs i WHERE i.id = jobs.id)) "
                f"WHERE id IN (SELECT id FROM incoming.jobs) AND status = 'new'"
            )
            refreshed = cur.rowcount

            self.conn.commit()
            after = cur.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            assert after - before == inserted
        finally:
            cur.execute("DETACH DATABASE incoming")

        return inserted, refreshed, new_ids + changed_ids
