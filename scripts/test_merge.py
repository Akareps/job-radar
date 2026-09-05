"""Offline CLI import regressions. Uses temporary databases and mocked git only.

Run: python scripts/test_merge.py
"""

import contextlib
import gzip
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobradar import cli  # noqa: E402
from jobradar.db import Store  # noqa: E402
from jobradar.models import Job, MAX_DESCRIPTION_CHARS  # noqa: E402
from jobradar.scoring import Scorer  # noqa: E402


def rows(path):
    """Inspect fixtures without opening the production store or changing state."""
    with contextlib.closing(sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro", uri=True,
    )) as conn:
        conn.row_factory = sqlite3.Row
        return {row["id"]: dict(row) for row in conn.execute("SELECT * FROM jobs")}


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.local = self.root / "local.db"
        self.source = self.root / "source.db"
        self.profile_path = self.root / "config" / "profile.yaml"
        self.profile_path.parent.mkdir()
        profile_source = ROOT / "config" / "profile.example.yaml"
        if not profile_source.exists():
            profile_source = ROOT / "config" / "profile.yaml"
        profile = yaml.safe_load(profile_source.read_text(encoding="utf-8"))
        # A distinctive local weight ensures foreign scores cannot pass as local.
        profile["role_families"]["quant_research"]["weight"] = 123
        self.profile_path.write_text(yaml.safe_dump(profile), encoding="utf-8")
        self.settings = {"database": str(self.local), "profile": str(self.profile_path)}
        self.settings_path = self.root / "settings.yaml"
        self.settings_path.write_text(yaml.safe_dump(self.settings), encoding="utf-8")
        self.scorer = Scorer(self.profile_path)

        self.changed = self.job("linkedin", "changed", "Old Title")
        self.unchanged = self.job("linkedin", "unchanged", "Data Scientist")
        self.duplicate = self.job("linkedin", "duplicate", "Graduate Quantitative Researcher")
        self.applied = self.job("linkedin", "applied", "Junior Software Engineer")
        self.rejected = self.job("linkedin", "rejected", "Junior Data Engineer")
        with Store(self.local) as store:
            store.upsert_jobs([
                self.changed, self.unchanged, self.duplicate, self.applied, self.rejected,
            ])
            store.set_status(self.applied.id, "applied")
            store.set_notes(self.applied.id, "Local application notes")
            store.set_status(self.rejected.id, "rejected")
            store.set_notes(self.rejected.id, "Local rejection notes")
            # An unchanged row must not be needlessly rescored on every sync.
            store.conn.execute("UPDATE jobs SET score=19 WHERE id=?", (self.unchanged.id,))
            store.conn.commit()

        self.remote_changed = self.job("linkedin", "changed", "Graduate Machine Learning Engineer")
        self.remote_duplicate = self.job("ats:greenhouse", "duplicate", self.duplicate.title)
        self.remote_long = self.job("ats:greenhouse", "long", "Junior Quantitative Researcher")
        foreign = [
            self.remote_changed, self.remote_duplicate, self.remote_long,
            self.unchanged, self.applied, self.rejected,
        ]
        with Store(self.source) as store:
            store.upsert_jobs(foreign)
            store.conn.execute(
                "UPDATE jobs SET score=9999, role_family='foreign', "
                "seniority='foreign', score_breakdown='{}', tags='[\"foreign\"]', "
                "notes='Foreign notes', dedupe_key='foreign'"
            )
            # Simulate a legacy source database from before the storage cap.
            store.conn.execute("UPDATE jobs SET description=? WHERE id=?",
                               ("Python. " * 2000, self.remote_long.id))
            store.conn.commit()
        self.source_bytes = self.source.read_bytes()
        self.source.chmod(0o444)
        self.addCleanup(self.source.chmod, 0o666)
        self.before = rows(self.local)
        self.touched = {self.remote_changed.id, self.remote_duplicate.id, self.remote_long.id}

    def job(self, source, sid, title):
        return self.scorer.score(Job(
            source=source, source_id=sid, title=title, company="Example Labs",
            url=f"https://example.invalid/{sid}", location="Zurich, Switzerland",
            description="Python engineering", language="",
        ))

    def assert_imported(self):
        after = rows(self.local)
        self.assertEqual(len(after), len(self.before) + 2)
        for protected in (self.applied, self.rejected):
            self.assertEqual(after[protected.id], self.before[protected.id])
        self.assertEqual(after[self.unchanged.id]["score"], 19)
        self.assertEqual(after[self.changed.id]["title"], self.remote_changed.title)
        for jid in self.touched:
            row = after[jid]
            expected = self.scorer.score(Job(
                source=row["source"], source_id=row["source_id"], title=row["title"],
                company=row["company"], url=row["url"], location=row["location"],
                description=row["description"], language="",
            ))
            self.assertEqual(row["score"], expected.score)
            self.assertEqual(row["role_family"], expected.role_family)
            self.assertEqual(row["seniority"], expected.seniority)
            self.assertEqual(json.loads(row["tags"]), expected.tags)
            self.assertEqual(json.loads(row["score_breakdown"]), expected.score_breakdown)
            self.assertEqual(row["dedupe_key"], expected.dedupe_key)
        self.assertEqual(after[self.duplicate.id]["is_primary"], 0)
        self.assertEqual(after[self.remote_duplicate.id]["is_primary"], 1)
        self.assertEqual(after[self.remote_long.id]["description"],
                         ("Python. " * 2000)[:MAX_DESCRIPTION_CHARS] + Store.TRUNCATION_MARKER)
        self.assertEqual(self.source.read_bytes(), self.source_bytes)

    def test_manual_merge_rebuilds_local_scores_and_preserves_triage(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(
            cli, "rescore_db", wraps=cli.rescore_db,
        ) as rescore:
            code = cli.main(["-c", str(self.settings_path), "merge", str(self.source)])
        self.assertEqual(code, 0)
        self.assertIn("2 new", output.getvalue())
        self.assertIn("3 rescored locally", output.getvalue())
        self.assertEqual(set(rescore.call_args.args[2]), self.touched)
        self.assert_imported()

    def test_cloud_sync_uses_the_same_merge_path_without_network(self):
        def fake_git(command, **kwargs):
            if command[1] == "show":
                self.assertTrue(command[2].endswith(":data/jobs.db.gz"))
                return subprocess.CompletedProcess(command, 0, gzip.compress(self.source_bytes), b"")
            self.assertIn(command[1], ("rev-parse", "fetch"))
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("subprocess.run", side_effect=fake_git), patch.object(
            cli, "merge_database", wraps=cli.merge_database,
        ) as merge:
            ok, message = cli.sync_from_remote(self.settings)
        self.assertTrue(ok, message)
        self.assertIn("3 rescored locally", message)
        merge.assert_called_once()
        self.assert_imported()

    def test_invalid_profile_fails_before_importing_any_rows(self):
        invalid = self.root / "invalid-profile.yaml"
        invalid.write_text("role_families: {}\n", encoding="utf-8")
        settings = {**self.settings, "profile": str(invalid)}
        self.settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            code = cli.main(["-c", str(self.settings_path), "merge", str(self.source)])
        self.assertEqual(code, 1)
        self.assertIn("local profile", error.getvalue())
        self.assertEqual(rows(self.local), self.before)
        self.assertEqual(self.source.read_bytes(), self.source_bytes)

    def test_invalid_source_fails_without_changing_either_database(self):
        corrupt = self.root / "corrupt.db"
        corrupt.write_bytes(b"not a SQLite database")
        corrupt.chmod(0o444)
        self.addCleanup(corrupt.chmod, 0o666)
        for source in (corrupt, self.root / "missing.db"):
            with self.subTest(source=source.name), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["-c", str(self.settings_path), "merge", str(source)])
                self.assertEqual(code, 1)
                self.assertEqual(rows(self.local), self.before)
        self.assertEqual(corrupt.read_bytes(), b"not a SQLite database")
        self.assertFalse((self.root / "missing.db").exists())
        self.assertEqual(self.source.read_bytes(), self.source_bytes)


if __name__ == "__main__":
    unittest.main()
