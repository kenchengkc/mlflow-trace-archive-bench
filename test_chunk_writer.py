import unittest
from importlib.util import find_spec

from chunk_ingest import payload_text, verify_ingested, write_trace
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

    def test_only_the_payload_attribute_changes_on_the_stored_span(self):
        with local_database() as (sql, uri), open_store(uri) as store:
            sql("""
                INSERT INTO experiments (experiment_id, name, artifact_location, lifecycle_stage)
                VALUES (1000, 'ingest-1000', 'file:///synthetic/1000', 'active'),
                       (1001, 'ingest-1001', 'file:///synthetic/1001', 'active');
            """)
            sizes = (0, 16 * 1024, 64 * 1024)
            summaries = []
            for size in sizes:
                sql("TRUNCATE trace_info, spans CASCADE")
                for index in range(2):
                    write_trace(store, 1, index, payload_text(1, index, size))
                summaries.append(verify_ingested(sql, 1, 2, payload_bytes=size)["payload"])

            self.assertEqual(len({s["base_content_sha256"] for s in summaries}), 1)
            small, large, larger = summaries
            self.assertGreater(large["logical_bytes_min"] - small["logical_bytes_min"], sizes[1])
            self.assertEqual(larger["logical_bytes_sum"] - large["logical_bytes_sum"], 2 * (sizes[2] - sizes[1]))
            self.assertGreater(larger["stored_bytes_min"], large["stored_bytes_min"])
            # Keep the field present and the same length, but change its bytes.
            sql("""
                UPDATE spans SET content = jsonb_set(
                    content::jsonb, '{attributes,benchmark.payload}',
                    to_jsonb(to_json(repeat('x', 65536))::text)
                )::text WHERE experiment_id = 1001;
            """)
            with self.assertRaisesRegex(RuntimeError, "Stored payload differs.*trace index 1"):
                verify_ingested(sql, 1, 2, payload_bytes=sizes[2])
