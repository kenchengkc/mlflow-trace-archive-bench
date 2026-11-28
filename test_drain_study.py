from contextlib import ExitStack
from dataclasses import replace
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from drain_fixture import seed_queue, verify_readback
from drain_measure import measure_drain
from local_postgres import local_database
from mlflow_driver import MLFLOW_VERSION, open_store
from test_driver_fixture import installed_mlflow


class OrmAttributionTest(unittest.TestCase):
    def test_result_hooks_only_partition_time_outside_earlier_timers(self):
        from finalize_probe import split_result_time

        windows = [(0, 100), (120, 140)]
        results = [(8, 15, "results"), (35, 65, "results"), (5, 85, "eligibility"),
                   (122, 132, "results")]
        queries = [(7, 20, "construction"), (31, 37, "dispatch")]
        orm = [(10, 12, "session"), (40, 45, "fetch")]
        drivers = [(11, 12, "sql"), (43, 44, "sql"), (58, 60, "sql"),
                   (80, 83, "sql"), (135, 139, "commit")]
        detail = split_result_time(windows, results, queries, orm, drivers)
        # The first result hook is fully masked by construction. The other two
        # receive 21 and 10 ns. Earlier timers cover 33 distinct ns across both windows.
        self.assertEqual(detail["calls"], {"eligibility": 1, "results": 3})
        self.assertEqual({k: round(ms * 1e6) for k, ms in detail["ms"].items()},
                         {"eligibility": 30, "results": 31, "other": 26})

    def test_query_hooks_only_partition_time_outside_driver_and_orm_calls(self):
        from finalize_probe import split_query_time

        windows = [(0, 100), (150, 160)]
        queries = [(13, 17, "compilation"), (12, 18, "compile_cache"), (5, 35, "construction"),
                   (42, 56, "compilation"), (37, 58, "compile_cache"),
                   (2, 90, "dispatch"), (92, 96, "construction")]
        orm = [(10, 28, "session"), (45, 50, "objects")]
        drivers = [(7, 9, "sql"), (14, 16, "sql"), (47, 49, "sql"),
                   (65, 70, "sql"), (151, 154, "commit")]
        result = split_query_time(windows, queries, orm, drivers)
        # The first compile lies entirely inside the session. Both compile
        # calls count, but only the second contributes time (14 - 5 = 9 ns).
        self.assertEqual(result["calls"], {"construction": 2, "compile_cache": 2, "compilation": 2, "dispatch": 1})
        self.assertEqual({k: round(ms * 1e6) for k, ms in result["ms"].items()},
                         {"construction": 14, "compile_cache": 7, "compilation": 9, "dispatch": 32, "other": 15})

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
    def test_metadata_guard_starts_at_the_matching_locked_header_return(self):
        from mlflow.store.tracking.dbmodels.models import SqlTraceInfo
        from mlflow.store.tracking.sqlalchemy_store import SqlAlchemyStore
        from sqlalchemy.orm import Query

        from finalize_probe import FinalizeProbe
        from result_probe import ResultProbe

        header, unrelated = SqlTraceInfo(), SqlTraceInfo()
        locked = Query(SqlTraceInfo).with_for_update()
        # A skipped guard can leave a header pending. A different header or a
        # later empty lookup must never donate its elapsed time to a new guard.
        clock = [0]
        returns = iter([header, header, header, None, header])

        def get_header(query):
            clock[0] += 10
            return next(returns)

        def metadata(store, row):
            clock[0] += 2
            return True

        def stale_generation(store):
            locked.one_or_none()
            return False

        finalizer = FinalizeProbe(None)
        probe = ResultProbe(finalizer)
        with ExitStack() as stack:
            stack.enter_context(patch.object(Query, "one_or_none", new=get_header))
            guard = "_is_trace_metadata_actionable_for_archival"
            stack.enter_context(patch.object(SqlAlchemyStore, guard, new=metadata))
            stack.enter_context(patch.object(SqlAlchemyStore, "_finalize_archived_trace", new=stale_generation))
            stack.enter_context(patch("result_probe.perf_counter_ns", new=lambda: clock[0]))
            probe.install(stack)
            check = getattr(SqlAlchemyStore, guard)
            finalizer.active = True
            locked.one_or_none()                  # locked lookup ends at 10
            clock[0] = 14
            check(None, header)                   # includes 4 ns of inline checks
            locked.one_or_none()
            clock[0] = 40
            check(None, unrelated)                # mismatched header
            check(None, header)                   # previous marker already consumed
            locked.one_or_none()
            self.assertIsNone(locked.one_or_none())
            clock[0] = 70
            check(None, header)                   # no header from the most recent lock
            self.assertFalse(SqlAlchemyStore._finalize_archived_trace(None))
            clock[0] = 90
            check(None, header)                   # the earlier finalizer returned before its metadata guard
        self.assertEqual(probe.events, [(10, 16, "eligibility"), (40, 42, "eligibility"),
                                       (42, 44, "eligibility"), (70, 72, "eligibility"), (90, 92, "eligibility")])

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
                    self.assertFalse(any(key.startswith("query_") for key in row))
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

    def test_query_costs_partition_orm_remainder_and_preserve_archival(self):
        categories = {"construction", "compile_cache", "compilation", "dispatch"}
        with local_database() as (sql, uri), open_store(uri) as store, TemporaryDirectory() as tmp:
            expected = seed_queue(store, 5)
            archive = Path(tmp).resolve()
            rows = measure_drain(store, archive.as_uri(), expected, profile_queries=True)
            # Trace 5 shares the first timestamp and moves to the next pass.
            batches = [[int(t.removeprefix("tr-d1a1"), 16) for t in row["selected_ids"]] for row in rows]
            self.assertEqual(batches, [[2, 3, 4], [5, 1], []])
            for row, count in zip(rows, (3, 2, 0), strict=True):
                with self.subTest(pass_number=row["pass_number"]):
                    self.assertNotIn("result_ms", row)
                    self.assertNotIn("result_calls", row)
                    self.assertEqual(row["archived_count"], count)
                    cache = row["query_cache_counts"]
                    misses = cache["cache_miss"]
                    if row["pass_number"] == 1:
                        self.assertGreater(misses, 0)
                        self.assertLess(misses, 8 * count)
                    else:
                        self.assertEqual(misses, 0)
                    self.assertEqual(row["query_calls"], {
                        "construction": 28 * count, "compile_cache": 8 * count,
                        "compilation": misses, "dispatch": 24 * count,
                    })
                    self.assertEqual(set(row["query_ms"]), categories | {"other"})
                    times_ns = {key: round(ms * 1e6) for key, ms in row["query_ms"].items()}
                    self.assertEqual(sum(times_ns.values()), round(row["orm_ms"]["other"] * 1e6))
                    for key in categories - {"compilation"}:
                        self.assertEqual(times_ns[key] > 0, bool(count))
                    self.assertGreaterEqual(times_ns["other"], 0)
                    self.assertEqual(cache, {
                        "cache_hit": 8 * count - misses, "cache_miss": misses, "caching_disabled": 0,
                        "no_cache_key": 0, "no_dialect_support": 0,
                    })
                    if not misses:
                        self.assertEqual(times_ns["compilation"], 0)
            self.assertEqual(verify_readback(store, sql, expected, archive), {
                "traces_read_back": 5, "spans_read_back": 10,
                "database_span_payloads_cleared": 10, "archive_files_verified": 5,
            })

    def test_eligibility_and_result_processing_partition_the_query_remainder(self):
        with local_database() as (sql, uri), open_store(uri) as store, TemporaryDirectory() as tmp:
            expected = seed_queue(store, 5)
            archive = Path(tmp).resolve()
            rows = measure_drain(store, archive.as_uri(), expected, profile_results=True)
            ids = [f"tr-d1a1{i:028x}" for i in (2, 3, 4, 5, 1)]
            self.assertEqual([row["selected_ids"] for row in rows], [ids[:3], ids[3:], []])
            for row, count in zip(rows, (3, 2, 0), strict=True):
                with self.subTest(pass_number=row["pass_number"]):
                    self.assertEqual(row["archived_count"], count)
                    self.assertEqual(row["result_calls"], {"eligibility": 2 * count, "results": 14 * count})
                    durations = {k: round(ms * 1e6) for k, ms in row["result_ms"].items()}
                    self.assertEqual(set(durations), {"eligibility", "results", "other"})
                    self.assertEqual(sum(durations.values()), round(row["query_ms"]["other"] * 1e6))
                    for key in row["result_calls"]:
                        self.assertEqual(durations[key] > 0, bool(count))
                    self.assertGreaterEqual(durations["other"], 0)
            self.assertEqual(verify_readback(store, sql, expected, archive), {
                "traces_read_back": 5, "spans_read_back": 10,
                "database_span_payloads_cleared": 10, "archive_files_verified": 5,
            })


if __name__ == "__main__":
    unittest.main()
