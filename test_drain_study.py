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


class OrmAttributionTest(unittest.TestCase):
    def test_nested_orm_and_driver_calls_share_no_time(self):
        from finalize_probe import split_orm_time

        windows = [(0, 60), (100, 160)]
        # Hooks record intervals on return. Include an ORM call nested in the same
        # category, a driver call outside the hooks, and one spanning adjacent hooks.
        orm = [(12, 18, "fetch"), (20, 26, "fetch"), (10, 40, "objects"), (4, 50, "session"),
               (107, 112, "objects"), (105, 120, "objects"), (125, 145, "session"), (145, 149, "fetch")]
        driver = [(1, 3, "sql"), (14, 17, "sql"), (30, 33, "sql"), (45, 49, "commit"),
                  (143, 148, "sql")]
        result = split_orm_time(windows, orm, driver)
        self.assertEqual(result["calls"], {"fetch": 3, "objects": 3, "session": 2})
        self.assertEqual({name: round(ms * 1e6) for name, ms in result["ms"].items()},
                         {"fetch": 10, "objects": 30, "session": 30, "other": 33})


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
                    self.assertNotIn("finalization_ms", row)
                    self.assertNotIn("finalization_calls", row)
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

    def test_finalization_statements_and_commit_for_each_archived_trace(self):
        with local_database() as (sql, uri), open_store(uri) as store, TemporaryDirectory() as tmp:
            expected = seed_queue(store, 5)
            archive = Path(tmp).resolve()
            passes = measure_drain(store, archive.as_uri(), expected, profile_finalization=True)
            # Suffixes 2–5 tie at the older timestamp; suffix 5 must precede suffix 1.
            suffixes = [[int(t.removeprefix("tr-d1a1"), 16) for t in p["selected_ids"]] for p in passes]
            self.assertEqual(suffixes, [[2, 3, 4], [5, 1], []])
            for row, count in zip(passes, (3, 2, 0), strict=True):
                with self.subTest(pass_number=row["pass_number"]):
                    self.assertNotIn("orm_ms", row)
                    self.assertNotIn("orm_calls", row)
                    calls = row["finalization_calls"]
                    durations = row["finalization_ms"]
                    expected_calls = dict.fromkeys((
                        "lock_trace", "read_trace_tags", "check_span_content", "clear_span_content",
                        "update_tag", "lookup_tag", "insert_tag", "delete_tag", "commit", "rollback",
                    ), count)
                    expected_calls["other_sql"] = 0
                    self.assertEqual(calls, expected_calls)
                    self.assertEqual(set(durations), set(calls) | {"other"})
                    for operation, n in calls.items():
                        self.assertEqual(durations[operation] > 0, n > 0)
                    self.assertGreaterEqual(durations["other"], 0)
                    self.assertAlmostEqual(sum(durations.values()), row["phase_ms"]["db_finalize"])
            self.assertEqual(verify_readback(store, sql, expected, archive)["spans_read_back"], 10)

    def test_orm_timings_account_for_the_remainder_after_driver_calls(self):
        categories = {"fetch", "objects", "session"}
        with local_database() as (sql, uri), open_store(uri) as store, TemporaryDirectory() as tmp:
            expected = seed_queue(store, 4)
            archive = Path(tmp).resolve()
            passes = measure_drain(store, archive.as_uri(), expected, profile_orm=True)
            ids = [f"tr-d1a1{i:028x}" for i in range(1, 5)]
            self.assertEqual([row["selected_ids"] for row in passes], [ids[:3], ids[3:], []])
            for row, count in zip(passes, (3, 1, 0), strict=True):
                with self.subTest(pass_number=row["pass_number"]):
                    self.assertEqual(row["archived_count"], count)
                    # Per trace the pinned path fetches four result sets. Object
                    # and session counts include nested calls to the timed methods.
                    self.assertEqual(row["orm_calls"], {
                        "fetch": 4 * count, "objects": 13 * count, "session": 22 * count,
                    })
                    self.assertEqual(set(row["orm_ms"]), categories | {"other"})
                    durations_ns = {key: round(ms * 1e6) for key, ms in row["orm_ms"].items()}
                    for key in categories:
                        if count:
                            self.assertGreater(durations_ns[key], 0)
                        else:
                            self.assertEqual(durations_ns[key], 0)
                    self.assertGreaterEqual(durations_ns["other"], 0)
                    if count == 0:
                        self.assertEqual(durations_ns["other"], 0)
                    self.assertEqual(sum(durations_ns.values()), round(row["finalization_ms"]["other"] * 1e6))
            self.assertEqual(verify_readback(store, sql, expected, archive), {
                "traces_read_back": 4, "spans_read_back": 8,
                "database_span_payloads_cleared": 8, "archive_files_verified": 4,
            })


if __name__ == "__main__":
    unittest.main()
