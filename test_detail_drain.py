from importlib.util import find_spec
import unittest

from mlflow_driver import MLFLOW_VERSION
from result_timing_study import compare
from test_driver_fixture import installed_mlflow


@unittest.skipUnless(installed_mlflow == MLFLOW_VERSION and find_spec("psycopg2"),
                     "requires the driver study's MLflow and psycopg2 dependencies")
class DetailedDrainTest(unittest.TestCase):
    def test_matched_queues_preserve_order_readback_and_both_timing_totals(self):
        reports = compare(rounds=1, traces=5)
        expected = [f"tr-d1a1{i:028x}" for i in (2, 3, 4, 5, 1)]
        for mode, report in reports.items():
            trial, = report["rounds"]
            self.assertEqual(trial["mode_order"], ["control", "detail", "overhead"])
            self.assertEqual([p["selected_ids"] for p in trial["passes"]], [expected[:3], expected[3:], []])
            self.assertEqual(trial["readback"], {
                "traces_read_back": 5, "spans_read_back": 10,
                "database_span_payloads_cleared": 10, "archive_files_verified": 5,
            })
            for row, count in zip(trial["passes"], (3, 2, 0), strict=True):
                with self.subTest(mode=mode, count=count):
                    self.assertEqual(row["archived_count"], count)
                    self.assertEqual("overhead_ms" in row, mode == "overhead")
                    self.assertEqual("result_detail_ms" in row, mode != "control")
                    if mode != "control":
                        self.assertEqual(row["result_detail_calls"], {
                            "setup": 6 * count, "filtering": 5 * count, "consumption": 3 * count,
                        })
                        self.assertEqual(sum(round(ms * 1e6) for ms in row["result_detail_ms"].values()),
                                         round(row["result_ms"]["results"] * 1e6))
                    if mode == "overhead":
                        calls = sum(sum(row[name].values()) for name in (
                            "finalization_calls", "orm_calls", "query_calls", "result_calls",
                        ))
                        self.assertEqual(row["overhead_calls"], {"bookkeeping": calls})
                        self.assertEqual(row["overhead_ms"]["bookkeeping"] > 0, count > 0)
                        self.assertEqual(sum(round(ms * 1e6) for ms in row["overhead_ms"].values()),
                                         round(row["result_ms"]["other"] * 1e6))
