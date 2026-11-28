import unittest

from finalize_probe import split_result_time


class TagSelectAccountingTest(unittest.TestCase):
    def test_only_exclusive_select_construction_is_split(self):
        windows = [(0, 100), (110, 135)]
        results = [(a, b, "consumption") for a, b in windows]
        scopes = {
            "consumption_events": [(a, b, "eager_tags") for a, b in windows],
            "tag_load_events": [(a, b, "preparation") for a, b in windows],
            "tag_prep_events": [(30, 35, "loader_options"), (7, 90, "select_construction"),
                                (110, 135, "parent_keys")],
        }
        args = (windows, results, [(10, 16, "construction"), (61, 63, "construction")],
                [(14, 18, "objects")], [(15, 17, "sql")])
        control = split_result_time(*args, **scopes)
        select = [(8, 26, "columns_bundle"), (27, 59, "select_from"), (60, 86, "in_predicate"),
                  (110, 135, "in_predicate")]
        detail = split_result_time(*args, **scopes, tag_select_events=select)
        self.assertEqual(control, {k: v for k, v in detail.items() if not k.startswith("tag_select_")})
        # Earlier SQL/ORM calls retain [10,18) and [61,63). Loader options own
        # [30,35). The second predicate event is masked by the parent-key scope.
        self.assertEqual(detail["tag_prep_ms"]["select_construction"], 68e-6)
        self.assertEqual({k: round(v * 1e6) for k, v in detail["tag_select_ms"].items()},
                         {"columns_bundle": 10, "select_from": 27, "in_predicate": 24, "other": 7})
        self.assertEqual(detail["tag_select_calls"], {"columns_bundle": 1, "select_from": 1, "in_predicate": 2})

    def test_select_breakdown_requires_preparation_events(self):
        with self.assertRaisesRegex(ValueError, "preparation"):
            split_result_time([], [], [], [], [], tag_select_events=[])
