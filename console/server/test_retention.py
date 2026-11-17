"""Persistent regressions for rolling telemetry, evidence retention and concurrent export."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from history import History


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "archive.db"

    def open(self, days, max_samples):
        with patch.dict(os.environ, {"EDGE_TELEMETRY_RETENTION_DAYS":str(days),
                                     "EDGE_TELEMETRY_MAX_SAMPLES":str(max_samples)}):
            return History(self.path)

    def record(self, db, kind="telemetry.sample", **keys):
        return db.record({"session_id":"archive", "missions":[]}, kind=kind, source="edge_console",
                         detail=kind, **keys)

    def test_existing_archive_survives_policy_change_and_restart(self):
        db = self.open(0, 0)
        # Use an existing-format archive without the new indexes.
        for name in ("events_cause","ordinary_by_seq","ordinary_by_age"):
            db.db.execute("DROP INDEX " + name)
        with patch("history.time.time", return_value=time.time()-12*86400):
            old = self.record(db)
            anchor = self.record(db)
            mission = self.record(db, "mission.created", mission_id="run", cause=anchor,
                                  source_id="core", source_seq=1)
            failure = self.record(db, "ros.log", level="error")
            warning = self.record(db, "source.stale", level="warn")
            tagged_sample = self.record(db, mission_id="run")
            abnormal_sample = self.record(db, level="error")
        first, second, third = [self.record(db) for _ in range(3)]
        db.close()
        db = self.open(7, 2)
        try:
            self.assertIsNone(db.replay(old))
            self.assertIsNone(db.replay(first))
            for seq in (anchor, mission, failure, warning, tagged_sample, abnormal_sample, second, third):
                self.assertEqual(db.replay(seq)["cursor"],seq)
            self.assertEqual(db.source_cursor("core"),1)
            self.assertEqual(db.retention()["earliest_cursor"],anchor)
            self.assertEqual(db.retention()["latest_cursor"],third)
            newest = self.record(db)
            self.assertIsNone(db.replay(second))
            self.assertIsNotNone(db.replay(third))
            self.assertEqual(db.retention()["latest_cursor"],newest)
        finally:
            db.close()

    def test_count_and_age_work_independently(self):
        db = self.open(1, 0)
        try:
            with patch("history.time.time",return_value=time.time()-2*86400):
                old = self.record(db)
            for _ in range(4):
                self.record(db)
            self.assertIsNone(db.replay(old))
            self.assertEqual(db.retention()["samples"],4)
        finally:
            db.close()
        db = self.open(0, 1)
        try:
            self.assertEqual(db.retention()["samples"],1)
        finally:
            db.close()

    def test_export_is_pinned_across_concurrent_pruning(self):
        db = self.open(0, 2)
        try:
            self.record(db,"source.online")
            first, second = [self.record(db) for _ in range(2)]
            export = db.export({}, include_snapshot=True)
            header = json.loads(next(export))
            self.record(db)
            self.assertIsNone(db.replay(first))
            remaining = [json.loads(line) for line in export]
            self.assertEqual([e["seq"] for e in remaining],[first, second])
            self.assertEqual([e["snapshot"]["cursor"] for e in remaining],[first, second])
            self.assertEqual(header["kind"],"source.online")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
