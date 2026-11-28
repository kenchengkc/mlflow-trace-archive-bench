import unittest

from finalize_probe import split_result_time


class TagLoadAccountingTest(unittest.TestCase):
    def test_tag_parts_only_receive_the_exclusive_eager_loader_budget(self):
        windows = [(0, 75), (90, 110)]
        results = [(10, 15, "setup"), (30, 32, "consumption"), (28, 35, "eligibility"),
                   (1, 67, "consumption"), (92, 109, "consumption")]
        rows = [(24, 27, "iteration"), (37, 43, "uniqueness"), (5, 62, "eager_tags"),
                (1, 65, "iteration"), (93, 100, "iteration"), (93, 100, "eager_tags")]
        # Grouping nested inside attachment owns [45, 49). The last grouping
        # call is masked by iteration finishing first at coincident boundaries.
        tags = [(6, 12, "preparation"), (16, 34, "grouping"), (45, 49, "grouping"),
                (42, 56, "attachment"), (93, 99, "grouping")]
        detail = split_result_time(windows, results, [(7, 9, "dispatch")], [(17, 20, "objects")],
                                   [(18, 23, "sql"), (106, 108, "sql")],
                                   consumption_events=rows, tag_load_events=tags)
        # Of the first grouping interval, [16,17), [23,24), [27,28), [30,32)
        # survive all earlier timers. The nested grouping contributes another 4 ns.
        self.assertEqual(detail["tag_load_calls"], {"preparation": 1, "grouping": 3, "attachment": 1})
        self.assertEqual({k: round(ms * 1e6) for k, ms in detail["tag_load_ms"].items()},
                         {"preparation": 2, "grouping": 9, "attachment": 9, "other": 10})
        self.assertEqual(detail["consumption_ms"]["eager_tags"], 30e-6)
