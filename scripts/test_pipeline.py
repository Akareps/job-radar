"""Offline scan lifecycle regressions; no files, database, or network are used.

Run: python scripts/test_pipeline.py
"""

import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import pipeline  # noqa: E402
from jobradar.models import Job  # noqa: E402


class ScanLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.store = Mock()
        self.store.upsert_jobs.return_value = (1, 0)
        self.store.mark_duplicates.return_value = 0
        self.store.query.return_value = []

        def start_scan():
            self.rows.append("running")
            return len(self.rows)

        self.store.start_scan.side_effect = start_scan
        self.store.finish_scan.side_effect = lambda scan_id, **_: self.rows.__setitem__(
            scan_id - 1, "finished"
        )
        self.store.fail_scan.side_effect = lambda scan_id, _: self.rows.__setitem__(
            scan_id - 1, "failed"
        )
        self.scorer = Mock()
        self.http = Mock()
        self.lock = Mock()
        self.lock.acquire.return_value = True
        self.source = Mock()
        self.source.name = "test"
        self.source.company = "Example"
        self.source.safe_fetch.return_value = ([Job(
            source="test", source_id="1", title="Example role",
            company="Example", url="https://example.invalid/1",
        )], None)
        self.settings = {
            "companies": "unused", "database": "unused", "profile": "unused",
            "workers": 1,
        }

        stack = ExitStack()
        self.addCleanup(stack.close)
        for target, replacement in (
            ("cfg.load_secrets", Mock(return_value={})),
            ("cfg.load_companies", Mock(return_value=[])),
            ("cfg.resolve", Mock(side_effect=lambda path: path)),
            ("build_sources", Mock(return_value=[self.source])),
            ("Store", Mock(return_value=self.store)),
            ("Scorer", Mock(return_value=self.scorer)),
            ("Http", Mock(return_value=self.http)),
            ("ScanLock", Mock(return_value=self.lock)),
            ("log", Mock()),
        ):
            stack.enter_context(patch("jobradar.pipeline." + target, replacement))

    def scan(self):
        return pipeline.run_scan(self.settings, skip_offline_check=True)

    def assert_closed(self):
        self.store.close.assert_called_once()
        self.http.session.close.assert_called_once()
        self.lock.release.assert_called_once()

    def assert_recorded_failure(self, failure):
        with self.assertRaises(type(failure)) as raised:
            self.scan()
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.rows, ["failed"])
        self.store.conn.rollback.assert_called_once()
        self.store.fail_scan.assert_called_once_with(
            1, f"{type(failure).__name__}: {failure}"
        )
        self.assert_closed()

    def test_success_closes_resources(self):
        summary = self.scan()
        self.assertEqual(summary["fetched"], 1)
        self.assertEqual(summary["errors"], [])
        self.assertEqual(self.rows, ["finished"])
        self.store.fail_scan.assert_not_called()
        self.store.conn.rollback.assert_not_called()
        self.assert_closed()

    def test_scoring_failure_is_recorded_and_next_scan_recovers(self):
        failure = ValueError("invalid scoring input")
        self.scorer.score.side_effect = failure
        self.assert_recorded_failure(failure)
        self.store.upsert_jobs.assert_not_called()

        self.scorer.score.side_effect = None
        self.assertEqual(self.scan()["fetched"], 1)
        self.assertEqual(self.rows, ["failed", "finished"])
        self.assertEqual(self.store.close.call_count, 2)
        self.assertEqual(self.lock.release.call_count, 2)

    def test_storage_failure_rolls_back_before_recording(self):
        failure = OSError("storage unavailable")
        self.store.upsert_jobs.side_effect = failure
        self.assert_recorded_failure(failure)
        methods = [call[0] for call in self.store.mock_calls]
        self.assertLess(methods.index("conn.rollback"), methods.index("fail_scan"))

    def test_summary_query_failure_is_recorded(self):
        failure = RuntimeError("summary query failed")
        self.store.query.side_effect = failure
        self.assert_recorded_failure(failure)

    def test_unexpected_worker_failure_is_recorded(self):
        failure = RuntimeError("worker failed")
        self.source.safe_fetch.side_effect = failure
        self.assert_recorded_failure(failure)

    def test_interruption_during_scoring_is_recorded(self):
        failure = KeyboardInterrupt()
        self.scorer.score.side_effect = failure
        self.assert_recorded_failure(failure)

    def test_recoverable_source_error_finishes_scan(self):
        self.source.safe_fetch.return_value = ([], "source unavailable")
        summary = self.scan()
        self.assertEqual(summary["errors"], ["test/Example: source unavailable"])
        self.assertEqual(self.rows, ["finished"])
        self.store.record_source.assert_called_once_with("test", 0, "source unavailable")
        self.assert_closed()

    def test_setup_failure_closes_store_before_any_scan_started(self):
        failure = ValueError("invalid profile")
        pipeline.Scorer.side_effect = failure
        with self.assertRaises(ValueError) as raised:
            self.scan()
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.rows, [])
        self.store.fail_scan.assert_not_called()
        self.store.close.assert_called_once()
        pipeline.Http.assert_not_called()
        self.lock.release.assert_called_once()

    def test_failure_recording_and_cleanup_do_not_hide_original_error(self):
        failure = ValueError("invalid scoring input")
        self.scorer.score.side_effect = failure
        self.store.conn.rollback.side_effect = OSError("rollback unavailable")
        self.store.fail_scan.side_effect = OSError("failure log unavailable")
        self.http.session.close.side_effect = OSError("session close failed")
        self.store.close.side_effect = OSError("store close failed")
        with self.assertRaises(ValueError) as raised:
            self.scan()
        self.assertIs(raised.exception, failure)
        self.store.fail_scan.assert_called_once()
        self.assert_closed()

    def test_failed_http_close_still_closes_store(self):
        self.http.session.close.side_effect = OSError("session close failed")
        with self.assertRaisesRegex(OSError, "session close failed"):
            self.scan()
        self.assert_closed()


if __name__ == "__main__":
    unittest.main(verbosity=2)
