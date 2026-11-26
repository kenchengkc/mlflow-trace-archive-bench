from importlib.util import find_spec
import unittest

from chunk_ingest import payload_text, verify_ingested, write_trace
from local_postgres import local_database
from mlflow_driver import MLFLOW_VERSION, open_store
from test_driver_fixture import installed_mlflow
from upsert_toast import toast_delta, toast_snapshot


class ToastDeltaTest(unittest.TestCase):
    def test_rejects_cross_database_snapshots_and_removed_values(self):
        before = {"relation_oid": 100, "database_oid": 5, "value_sizes": [[2, 2500, 1]],
                  "heap_bytes": 8192, "index_bytes": 8192}
        with self.assertRaisesRegex(ValueError, "different relations"):
            toast_delta(before, {**before, "database_oid": 6}, 1, 65536)
        with self.assertRaisesRegex(ValueError, "disappeared"):
            toast_delta(before, {**before, "value_sizes": [[34, 65964, 2]]}, 1, 65536)
        with self.assertRaisesRegex(ValueError, "externally stored"):
            toast_delta(before, before, 1, 65536)


@unittest.skipUnless(installed_mlflow == MLFLOW_VERSION and find_spec("psycopg2"),
                     "requires the driver study's MLflow and psycopg2 dependencies")
class StoredChunksTest(unittest.TestCase):
    def test_excludes_seed_and_warmup_and_reads_the_external_chunks(self):
        with local_database() as (sql, uri), open_store(uri) as store:
            sql("""INSERT INTO experiments (experiment_id, name, artifact_location, lifecycle_stage)
                   VALUES (1000, 'even', 'file:///synthetic/even', 'active'),
                          (1001, 'odd', 'file:///synthetic/odd', 'active')""")
            write_trace(store, 4, 0, payload_text(4, 0, 32000))
            for series, size in [(1, 0), (2, 65536)]:
                write_trace(store, 5 + series, 0, payload_text(5 + series, 0, size))
                before = toast_snapshot(sql)
                for index in range(2):
                    write_trace(store, series, index, payload_text(series, index, size))
                after = toast_snapshot(sql)
                delta = toast_delta(before, after, 2, size)
                payload = verify_ingested(sql, series, 2, payload_bytes=size)["payload"]
                if size == 0:
                    self.assertEqual((delta["external_values"], delta["chunks"], delta["chunk_bytes"]), (0, 0, 0))
                else:
                    self.assertEqual(delta["external_values"], 2)
                    self.assertGreater(delta["chunks"], 2)
                    self.assertEqual(delta["chunk_bytes"], payload["stored_bytes_sum"])
                    self.assertEqual(sum(b * n for _, b, n in delta["value_sizes"]), delta["chunk_bytes"])


if __name__ == "__main__":
    unittest.main()
