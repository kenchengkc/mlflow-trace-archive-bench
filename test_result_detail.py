import unittest

from finalize_probe import split_overhead_time, split_result_time


class ResultDetailTest(unittest.TestCase):
    def test_nested_result_categories_preserve_the_prior_result_total(self):
        windows = [(0, 100)]
        queries = [(0, 5, "dispatch"), (14, 16, "compilation")]
        orm = [(22, 26, "objects")]
        drivers = [(38, 42, "sql"), (55, 65, "sql")]
        events = [(12, 20, "filtering"), (10, 40, "consumption"), (6, 45, "eligibility"),
                  (50, 70, "setup"), (71, 80, "filtering")]
        detail = split_result_time(windows, events, queries, orm, drivers, profile_detail=True)
        ns = {k: round(ms * 1e6) for k, ms in detail["result_detail_ms"].items()}
        self.assertEqual(ns, {"setup": 10, "filtering": 15, "consumption": 16})
        self.assertEqual(detail["result_detail_calls"], {"setup": 1, "filtering": 2, "consumption": 1})
        legacy_events = [(s, e, k if k == "eligibility" else "results") for s, e, k in events]
        legacy = split_result_time(windows, legacy_events, queries, orm, drivers)
        self.assertEqual(detail["ms"], legacy["ms"])
        self.assertEqual(detail["calls"], legacy["calls"])
        self.assertEqual(legacy["ms"], {"eligibility": 7e-6, "results": 41e-6, "other": 27e-6})

    def test_bookkeeping_only_gets_unassigned_parts_of_nested_wrapper_envelopes(self):
        # [10, 50] encloses [15, 35]. All earlier measured calls retain their
        # time, including a driver call across the outer envelope's end.
        envelopes = [(15, 35, "bookkeeping"), (10, 50, "bookkeeping"), (55, 80, "bookkeeping")]
        results = [(17, 32, "setup"), (12, 40, "consumption"), (60, 71, "eligibility")]
        queries = [(3, 8, "dispatch"), (30, 34, "compile_cache")]
        orm = [(35, 45, "objects")]
        drivers = [(48, 52, "sql")]
        part = split_overhead_time([(0, 85)], envelopes, results, queries, orm, drivers)
        # Overhead spans [10,12], [45,48], [55,60], [71,80] = 19 ns.
        self.assertEqual(part["ms"], {"bookkeeping": 19e-6, "other": 13e-6})
        self.assertEqual(part["calls"], {"bookkeeping": 3})
