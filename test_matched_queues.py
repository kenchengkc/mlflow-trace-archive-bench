import unittest

from local_postgres import local_postgres
from matched_queues import inspect_queue
from test_candidates import FIXTURE, SQL_DIR


class QueueOracleTest(unittest.TestCase):
    def test_header_rank_prefix_and_unfilled_batch(self):
        with local_postgres() as sql:
            sql((SQL_DIR / "schema.sql").read_text())
            sql(FIXTURE)
            full = inspect_queue(sql, 3)
            self.assertEqual(full["matching_headers"], 8)
            self.assertEqual(full["kth_eligible_header_position"], 7)
            self.assertEqual(full["eligible_in_prefix"], 2)
            self.assertEqual(full["expected_rows"], [
                "first|2|1699999999998", "tie-a|8|1699999999999", "tie-b|1|1699999999999",
            ])
            strata = [(g["experiment_id"], g["span_count"], g["archived"], g["remaining"])
                      for g in full["strata"]]
            self.assertEqual(strata, [
                (1, 1, 0, 1), (2, 2, 0, 1), (3, 1, 0, 1), (5, 1, 1, 0), (8, 2, 0, 1),
            ])
            partial = inspect_queue(sql, 10)
            self.assertIsNone(partial["kth_eligible_header_position"])
            self.assertEqual(partial["eligible_in_prefix"], 4)
            self.assertEqual(partial["strata"], full["strata"])
