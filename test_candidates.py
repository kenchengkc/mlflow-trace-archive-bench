import unittest

from benchmark import QUERY, SQL_DIR
from local_postgres import local_postgres


# Insert the tied candidates in reverse ID order. Give each excluded trace a
# single reason to be skipped, so dropping any predicate changes the answer.
FIXTURE = """
INSERT INTO trace_info (request_id, experiment_id, timestamp_ms, status) VALUES
    ('tie-b',            1, 1699999999999, 'OK'),
    ('tie-a',            8, 1699999999999, 'ERROR'),
    ('first',            2, 1699999999998, 'OK'),
    ('on-cutoff',        3, 1700000000000, 'OK'),
    ('after-cutoff',     3, 1700000000001, 'OK'),
    ('other-experiment', 9, 1699999999990, 'OK'),
    ('still-running',    2, 1699999999990, 'IN_PROGRESS'),
    ('missing-span',     4, 1699999999990, 'OK'),
    ('empty-span',       4, 1699999999990, 'OK'),
    ('archive-location', 5, 1699999999990, 'OK'),
    ('failed-archive',   6, 1699999999990, 'OK');

INSERT INTO spans (trace_id, span_id, content)
SELECT request_id, 'root', CASE WHEN request_id = 'empty-span' THEN '' ELSE '{}' END
FROM trace_info WHERE request_id <> 'missing-span';
INSERT INTO spans (trace_id, span_id, content) VALUES
    ('first', 'child', '{}'),
    ('tie-a', 'empty-child', '');

INSERT INTO trace_tags (request_id, key, value) VALUES
    ('tie-a',            'mlflow.trace.spansLocation', 'TRACKING_STORE'),
    ('archive-location', 'mlflow.trace.spansLocation', 'ARCHIVE_REPO'),
    ('failed-archive',   'mlflow.trace.archivalFailure', NULL),
    ('first',            'other.tag', 'present');
"""


class CandidateQueryTest(unittest.TestCase):
    def test_eligible_rows_in_order_with_and_without_index(self):
        expected = [
            "first|2|1699999999998",
            "tie-a|8|1699999999999",
            "tie-b|1|1699999999999",
            "on-cutoff|3|1700000000000",
        ]
        with local_postgres() as sql:
            sql((SQL_DIR / "schema.sql").read_text())
            sql(FIXTURE)
            for indexed in (False, True):
                if indexed:
                    sql(
                        "CREATE INDEX index_trace_info_timestamp_ms_request_id "
                        "ON trace_info (timestamp_ms, request_id)"
                    )
                for batch_size in (10, 2, 0):
                    with self.subTest(indexed=indexed, batch_size=batch_size):
                        rows = sql(QUERY, variables={"batch_size": batch_size}).splitlines()
                        self.assertEqual(rows, expected[:batch_size])


if __name__ == "__main__":
    unittest.main()
