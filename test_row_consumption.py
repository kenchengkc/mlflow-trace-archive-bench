from contextlib import ExitStack
from importlib.util import find_spec
from types import SimpleNamespace
import unittest

from mlflow_driver import MLFLOW_VERSION
from result_timing_study import compare
from test_driver_fixture import installed_mlflow


@unittest.skipUnless(installed_mlflow == MLFLOW_VERSION and find_spec("psycopg2"),
                     "requires the driver study's MLflow and psycopg2 dependencies")
class RowConsumptionTest(unittest.TestCase):
    def test_three_trace_pass_then_partial_and_empty_match_control(self):
        reports = compare(rounds=1, traces=5, study="consumption")
        ids = [f"tr-d1a1{i:028x}" for i in (2, 3, 4, 5, 1)]
        for mode, report in reports.items():
            trial, = report["rounds"]
            self.assertEqual(trial["mode_order"], ["control", "detail"])
            self.assertEqual([p["selected_ids"] for p in trial["passes"]], [ids[:3], ids[3:], []])
            self.assertEqual(trial["readback"], {
                "traces_read_back": 5, "archive_files_verified": 5,
                "spans_read_back": 10, "database_span_payloads_cleared": 10,
            })
            for row, count in zip(trial["passes"], (3, 2, 0), strict=True):
                with self.subTest(mode=mode, count=count):
                    self.assertEqual(row["archived_count"], count)
                    self.assertEqual(row["result_detail_calls"]["consumption"], 3 * count)
                    if mode == "control":
                        self.assertFalse(any(k.startswith("consumption_") for k in row))
                        self.assertNotIn("eager_tag_load_ms", row)
                        continue
                    ns = {k: round(ms * 1e6) for k, ms in row["consumption_ms"].items()}
                    self.assertEqual(set(ns), {"iteration", "uniqueness", "eager_tags", "other"})
                    self.assertEqual(sum(ns.values()), round(row["result_detail_ms"]["consumption"] * 1e6))
                    self.assertTrue(all(value >= 0 for value in ns.values()))
                    # One header is unique, two rows are returned and two advances exhaust their result.
                    self.assertEqual(row["consumption_calls"], {
                        "iteration": 4 * count, "uniqueness": count, "eager_tags": count,
                    })
                    self.assertEqual(row["consumption_counts"], {
                        "rows_returned": 2 * count, "exhausted": 2 * count, "extra_rows_compared": 0,
                    })
                    for k in row["consumption_calls"]:
                        self.assertEqual(ns[k] > 0, count > 0)
                    self.assertGreaterEqual(round(row["eager_tag_load_ms"] * 1e6), ns["eager_tags"])
                    self.assertGreaterEqual(row["eager_tag_load_ms"], row["finalization_ms"]["read_trace_tags"])
                    self.assertEqual(row["eager_tag_load_ms"] > 0, count > 0)

    def test_custom_uniqueness_key_duplicate_distinct_and_empty_results(self):
        from sqlalchemy.engine.result import IteratorResult, SimpleResultMetaData
        from sqlalchemy.exc import MultipleResultsFound

        from consumption_probe import ConsumptionProbe

        probe = ConsumptionProbe(SimpleNamespace(active=True))
        with ExitStack() as stack:
            probe.install(stack)
            for values, has_distinct_row in [([], False), ([1], False), ([1, 11], False), ([1, 2], True)]:
                with self.subTest(values=values):
                    probe.reset()
                    calls = []

                    def key(row):
                        calls.append(row[0])
                        return row[0] % 10

                    result = IteratorResult(SimpleResultMetaData(["value"]), iter((v,) for v in values))
                    unique_rows = result.scalars().unique(key)
                    if has_distinct_row:
                        with self.assertRaises(MultipleResultsFound):
                            unique_rows.one_or_none()
                    else:
                        self.assertEqual(unique_rows.one_or_none(), values[0] if values else None)
                    self.assertEqual(calls, values)
                    categories = [category for _, _, category in probe.events]
                    self.assertEqual("uniqueness" in categories, bool(values))
                    self.assertNotIn("eager_tags", categories)
                    self.assertEqual(probe.counts["extra_rows_compared"], max(0, len(values) - 1))
