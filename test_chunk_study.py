import unittest
from importlib.util import find_spec

from local_postgres import local_database
from mlflow_driver import MLFLOW_VERSION, open_store
from test_driver_fixture import installed_mlflow


@unittest.skipUnless(installed_mlflow == MLFLOW_VERSION and find_spec("psycopg2"),
                     "requires the driver study's MLflow and psycopg2 dependencies")
class ChunkBoundaryTest(unittest.TestCase):
    def test_global_limit_splits_a_tie_across_query_chunks(self):
        from chunk_fixture import CUTOFF, prepare_chunk_fixture
        from chunk_study import verify_scope

        # Hand-derive both batches. The two oldest anchors have larger IDs than
        # all tied rows at the next timestamp, so sorting by ID alone also fails.
        first_anchor = (200001, 1000, CUTOFF - 2)
        second_anchor = (200000, 1001, CUTOFF - 2)
        first_batch = [first_anchor] + [(n, 1000, CUTOFF - 1) for n in range(2, 200, 2)]
        merged_batch = [second_anchor, first_anchor] + [
            (n, 1001 if n % 2 else 1000, CUTOFF - 1) for n in range(1, 99)
        ]
        ids = [str(n) for n in range(1, 1002)]
        cases = (
            (ids[:-1], first_batch, [1000], [100], (200, 1000)),
            (ids, merged_batch, [1000, 1], [50, 50], (99, 1001)),
            # Swap only the tied experiments so the other one becomes the singleton.
            (ids[:-2] + ids[-2:][::-1], merged_batch, [1000, 1], [50, 50], (99, 1001)),
        )
        with local_database() as (sql, uri), open_store(uri) as store:
            prepare_chunk_fixture(sql)
            for selected, expected, sizes, contributions, excluded in cases:
                with self.subTest(count=len(selected), last=selected[-1]):
                    result = verify_scope(store, selected)
                    rows = [(f"tr-{n:032x}", experiment, ts) for n, experiment, ts in expected]
                    self.assertEqual(result["ordered_rows"], rows)
                    self.assertEqual(result["first_excluded"],
                                     (f"tr-{excluded[0]:032x}", excluded[1], CUTOFF - 1))
                    queries = result["queries"]
                    self.assertEqual(result["query_count"], len(sizes))
                    self.assertEqual([q["experiment_count"] for q in queries], sizes)
                    self.assertEqual([q["candidate_limit"] for q in queries], [100] * len(sizes))
                    self.assertEqual([q["rows_fetched"] for q in queries], [100] * len(sizes))
                    self.assertEqual([q["rows_in_final_batch"] for q in queries], contributions)
                    self.assertEqual(queries[-1]["last_experiment_id"], int(selected[-1]))
