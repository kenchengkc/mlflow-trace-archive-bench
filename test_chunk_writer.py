import unittest
from importlib.util import find_spec

from chunk_ingest import verify_ingested, write_trace
from local_postgres import local_database
from mlflow_driver import MLFLOW_VERSION, open_store
from test_driver_fixture import installed_mlflow


@unittest.skipUnless(installed_mlflow == MLFLOW_VERSION and find_spec("psycopg2"),
                     "requires the driver study's MLflow and psycopg2 dependencies")
class ChunkWriterTest(unittest.TestCase):
    def test_counts_distinct_committed_headers_and_finished_root_spans(self):
        with local_database() as (sql, uri), open_store(uri) as store:
            sql("""
                INSERT INTO experiments (experiment_id, name, artifact_location, lifecycle_stage)
                VALUES (1000, 'ingest-1000', 'file:///synthetic/1000', 'active'),
                       (1001, 'ingest-1001', 'file:///synthetic/1001', 'active');
            """)
            write_trace(store, series=2, index=0)  # Untimed warm-up namespace.
            for index in range(2):
                write_trace(store, series=1, index=index)
            self.assertEqual(verify_ingested(sql, 1, 2), {"trace_headers": 2, "root_spans": 2})
            self.assertEqual(verify_ingested(sql, 2, 1), {"trace_headers": 1, "root_spans": 1})
            sql("DELETE FROM spans WHERE experiment_id = 1001")
            with self.assertRaisesRegex(RuntimeError, "Expected 2 committed traces"):
                verify_ingested(sql, 1, 2)
