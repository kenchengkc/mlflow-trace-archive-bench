import unittest

from finalize_probe import FinalizeProbe, split_result_time


class ConsumptionAccountingTest(unittest.TestCase):
    def test_only_innermost_operations_inside_the_consumption_budget_receive_time(self):
        windows = [(0, 100), (150, 170)]
        results = [
            (15, 20, "setup"), (25, 30, "consumption"), (23, 32, "filtering"),
            (10, 45, "consumption"), (7, 50, "eligibility"),
            (65, 75, "setup"), (65, 75, "consumption"), (60, 85, "consumption"),
            (150, 162, "consumption"),
        ]
        rows = [
            (26, 27, "uniqueness"), (21, 33, "eager_tags"), (11, 35, "iteration"),
            (42, 50, "uniqueness"), (65, 75, "eager_tags"), (64, 80, "iteration"),
            (155, 160, "iteration"), (150, 161, "uniqueness"),
        ]
        queries = [(36, 43, "dispatch")]
        orm = [(34, 37, "objects"), (154, 157, "session")]
        drivers = [(12, 16, "sql"), (28, 29, "sql"), (158, 164, "sql")]
        detail = split_result_time(windows, results, queries, orm, drivers, profile_detail=True,
                                   consumption_events=rows)
        # A consumption call nested inside filtering regains [25,28) and [29,30).
        # Setup finishing first at the coincident [65,75) boundaries masks the eager hook.
        self.assertEqual(detail["consumption_calls"], {"iteration": 3, "uniqueness": 3, "eager_tags": 2})
        self.assertEqual({k: round(ms * 1e6) for k, ms in detail["consumption_ms"].items()},
                         {"iteration": 10, "uniqueness": 7, "eager_tags": 6, "other": 10})
        self.assertEqual(detail["result_detail_ms"]["consumption"], 33e-6)

    def test_inclusive_tag_load_must_enclose_its_driver_query(self):
        probe = FinalizeProbe(None)
        probe.events = [(50, 55, "sql", "SELECT key FROM trace_tags WHERE trace_tags.request_id IN (x)"),
                        (85, 87, "commit", None)]

        def summarize(load):
            return probe.summarize([("db_finalize", 0, 100)], orm_events=[], query_events=[],
                                   result_events=[(2, 90, "consumption")], consumption_events=[load])

        report = summarize((45, 65, "eager_tags"))
        self.assertEqual(report["eager_tag_load_ms"], 20e-6)
        self.assertEqual(report["consumption_ms"]["eager_tags"], 15e-6)
        with self.assertRaisesRegex(RuntimeError, "trace-tag SELECT"):
            summarize((45, 53, "eager_tags"))
