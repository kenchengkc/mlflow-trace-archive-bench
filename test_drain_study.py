from dataclasses import replace
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from drain_fixture import seed_queue, verify_readback
from drain_measure import measure_drain
from local_postgres import local_database
from mlflow_driver import MLFLOW_VERSION, open_store
from test_driver_fixture import installed_mlflow


@unittest.skipUnless(installed_mlflow == MLFLOW_VERSION and find_spec("psycopg2"),
                     "requires the driver study's MLflow and psycopg2 dependencies")
class DrainStudyTest(unittest.TestCase):
    def test_ties_across_passes_partial_batch_and_archived_child_readback(self):
        with local_database() as (sql, uri), open_store(uri) as store, TemporaryDirectory() as tmp:
            expected = seed_queue(store, 8)
            archive = Path(tmp).resolve()
            passes = measure_drain(store, archive.as_uri(), expected)
            # The older timestamp covers suffixes 5–8; both timestamps span batch boundaries.
            batches = [[int(t.removeprefix("tr-d1a1"), 16) for t in row["selected_ids"]] for row in passes]
            self.assertEqual(batches, [[5, 6, 7], [8, 1, 2], [3, 4], []])
            self.assertEqual([row["archived_count"] for row in passes], [3, 3, 2, 0])
            for row in passes:
                self.assertEqual(set(row), {
                    "pass_number", "archived_count", "selected_ids", "selection_ms", "total_ms",
                })
                self.assertGreater(row["selection_ms"], 0)
                self.assertLessEqual(row["selection_ms"], row["total_ms"])
            self.assertEqual(verify_readback(store, sql, expected, archive), {
                "traces_read_back": 8, "spans_read_back": 16,
                "database_span_payloads_cleared": 16, "archive_files_verified": 8,
            })
            # A correct root span alone must not conceal a different child payload.
            first_id = next(iter(expected))
            root, child = expected[first_id].spans
            expected[first_id] = replace(expected[first_id], spans=[root, dict(child, name="other-child")])
            with self.assertRaisesRegex(RuntimeError, "span readback"):
                verify_readback(store, sql, expected, archive)

    def test_phase_times_reconcile_for_full_partial_and_empty_passes(self):
        phases = {"span_load", "serialization", "artifact_write", "db_finalize"}
        with local_database() as (sql, uri), open_store(uri) as store, TemporaryDirectory() as tmp:
            expected = seed_queue(store, 4)
            archive = Path(tmp).resolve()
            passes = measure_drain(store, archive.as_uri(), expected, profile_phases=True)
            # Four tied traces were inserted in descending trace-ID order.
            ids = [f"tr-d1a1{i:028x}" for i in range(1, 5)]
            self.assertEqual([p["selected_ids"] for p in passes], [ids[:3], ids[3:], []])
            for row, count in zip(passes, (3, 1, 0), strict=True):
                with self.subTest(pass_number=row["pass_number"]):
                    self.assertEqual(row["archived_count"], count)
                    self.assertEqual(row["phase_calls"], dict.fromkeys(phases, count))
                    durations = row["phase_ms"]
                    self.assertEqual(set(durations), phases | {"other"})
                    for phase in phases:
                        if count:
                            self.assertGreater(durations[phase], 0)
                        else:
                            self.assertEqual(durations[phase], 0)
                    self.assertGreaterEqual(durations["other"], 0)
                    self.assertAlmostEqual(row["selection_ms"] + sum(durations.values()), row["total_ms"])
            self.assertEqual(verify_readback(store, sql, expected, archive), {
                "traces_read_back": 4, "spans_read_back": 8,
                "database_span_payloads_cleared": 8, "archive_files_verified": 4,
            })


if __name__ == "__main__":
    unittest.main()
