import unittest

from finalize_probe import split_result_time


class TagPreparationAccountingTest(unittest.TestCase):
    def test_parts_split_only_exclusive_query_preparation(self):
        windows = [(0, 100), (110, 120)]
        results = [(28, 31, "eligibility"), (0, 100, "consumption"), (110, 120, "consumption")]
        rows = [(36, 42, "iteration"), (0, 90, "eager_tags"), (110, 120, "eager_tags")]
        tags = [(45, 50, "attachment"), (5, 80, "preparation"), (110, 120, "grouping")]
        prep = [(20, 24, "loader_options"), (5, 25, "select_construction"),
                (25, 40, "loader_options"), (40, 67, "parent_keys"), (110, 120, "parent_keys")]
        inputs = (windows, results, [(8, 13, "dispatch")], [(12, 18, "objects")], [(11, 16, "sql")])
        control = split_result_time(*inputs, consumption_events=rows, tag_load_events=tags)
        detail = split_result_time(*inputs, consumption_events=rows, tag_load_events=tags, tag_prep_events=prep)
        self.assertEqual(control, {k: v for k, v in detail.items() if not k.startswith("tag_prep_")})
        # [8,18) belongs to existing SQL/ORM timers. Nested options own [20,24).
        # Eligibility, iteration and attachment mask additional intervals. The
        # final parent-key call is inside grouping and receives zero time.
        self.assertEqual(detail["tag_load_ms"]["preparation"], 51e-6)
        self.assertEqual({k: round(v * 1e6) for k, v in detail["tag_prep_ms"].items()},
                         {"select_construction": 6, "loader_options": 12, "parent_keys": 20, "other": 13})
        self.assertEqual(detail["tag_prep_calls"], {"select_construction": 1, "loader_options": 2, "parent_keys": 2})
