from importlib.util import find_spec
import re
import unittest

from chunk_ingest import payload_text, write_trace
from local_postgres import local_database
from mlflow_driver import MLFLOW_VERSION, capture_statements, open_store
from test_driver_fixture import installed_mlflow
from upsert_server import prepare_server_stats, upsert_delta, upsert_snapshot


class CounterDeltaTest(unittest.TestCase):
    def test_requires_the_same_entry_and_exact_calls_and_rows_after_warmup(self):
        before = dict(dbid=20, userid=10, queryid="123", query="upsert", toplevel=True,
                      stats_reset="epoch", dealloc=0, calls=1, rows=1, total_exec_time_ms=1.0,
                      wal_records=3, wal_fpi=1, wal_bytes=500)
        after = dict(before, calls=3, rows=3, total_exec_time_ms=5.0,
                     wal_records=9, wal_fpi=1, wal_bytes=2500)
        self.assertEqual(upsert_delta(before, after, 2), {
            "calls": 2, "rows": 2, "total_exec_time_ms": 4.0,
            "wal_records": 6, "wal_fpi": 0, "wal_bytes": 2000,
        })
        for field in ("queryid", "dbid", "userid", "stats_reset", "dealloc", "rows", "calls", "wal_records"):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                upsert_delta(before, {**after, field: -1}, 2)
        with self.assertRaisesRegex(RuntimeError, "warm-up"):
            upsert_delta({**before, "calls": 0}, after, 2)


@unittest.skipUnless(installed_mlflow == MLFLOW_VERSION and find_spec("psycopg2"),
                     "requires the driver study's MLflow and psycopg2 dependencies")
class LiveUpsertStatsTest(unittest.TestCase):
    def test_tracks_only_the_driver_upsert_and_excludes_warmup(self):
        with local_database(preload_pg_stat_statements=True) as (sql, uri), open_store(uri) as store:
            prepare_server_stats(sql)
            sql("""INSERT INTO experiments (experiment_id, name, artifact_location, lifecycle_stage)
                   VALUES (1000, 'a', 'file:///test/1000', 'active'), (1001, 'b', 'file:///test/1001', 'active');
                   CREATE TABLE unrelated_writes (value text);""")
            with self.assertRaisesRegex(RuntimeError, "found 0"):
                upsert_snapshot(sql)
            with capture_statements(store.engine) as statements:
                write_trace(store, 2, 0)
            before = upsert_snapshot(sql)
            (upsert,) = [statement for statement, _ in statements if statement.startswith("INSERT INTO spans ")]
            # The driver uses psycopg2 named binds, PostgreSQL normalizes the literal
            # values in the executed query to $1, $2, ... in source order.
            self.assertEqual(re.sub(r"%\([^)]+\)s", "?", upsert), re.sub(r"\$\d+", "?", before["query"]))
            sql("INSERT INTO unrelated_writes SELECT repeat('x', 4096) FROM generate_series(1, 128)")
            self.assertEqual(upsert_snapshot(sql), before)
            write_trace(store, 1, 0)
            write_trace(store, 1, 1, payload_text(1, 1, 65536))
            delta = upsert_delta(before, upsert_snapshot(sql), 2)
            self.assertEqual((delta["calls"], delta["rows"]), (2, 2))
            self.assertGreater(delta["total_exec_time_ms"], 0)
            self.assertGreater(delta["wal_bytes"], 65536)


if __name__ == "__main__":
    unittest.main()
