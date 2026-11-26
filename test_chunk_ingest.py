import unittest
from importlib.util import find_spec

from chunk_fixture import BATCH_SIZE, CUTOFF, EXPERIMENT_COUNT, expected_candidates, prepare_chunk_fixture
from chunk_study import candidate_rows
from local_postgres import local_database
from mlflow_driver import MLFLOW_VERSION, find_candidates, open_store
from test_driver_fixture import installed_mlflow


@unittest.skipUnless(installed_mlflow == MLFLOW_VERSION and find_spec("psycopg2"),
                     "requires the driver study's MLflow and psycopg2 dependencies")
class LiveChunkOrderTest(unittest.TestCase):
    def test_commit_between_reads_changes_only_the_unread_chunk(self):
        from sqlalchemy import event

        ids = [str(i) for i in range(1, EXPERIMENT_COUNT + 1)]
        before = expected_candidates(ids)
        # Both arrivals tie with the two earliest fixture rows and have smaller IDs.
        first_chunk = (f"tr-{199998:032x}", 1000, CUTOFF - 2)
        last_chunk = (f"tr-{199999:032x}", 1001, CUTOFF - 2)
        chunks = []

        with local_database() as (sql, uri), open_store(uri) as store:
            prepare_chunk_fixture(sql)
            with store.ManagedSessionMaker() as session:
                self.assertEqual(candidate_rows(find_candidates(store, session, ids, CUTOFF, BATCH_SIZE)),
                                 before)

            with store.ManagedSessionMaker() as session, store.engine.connect() as writer:
                reader = session.connection()
                self.assertEqual(reader.exec_driver_sql("SHOW transaction_isolation").scalar_one(),
                                 "read committed")
                self.assertNotEqual(reader.connection.driver_connection.get_backend_pid(),
                                    writer.connection.driver_connection.get_backend_pid())

                def commit_after_first_select(conn, cursor, statement, parameters, context, executemany):
                    self.assertTrue(statement.lstrip().upper().startswith("SELECT"))
                    chunks.append({value for key, value in parameters.items()
                                   if key.startswith("experiment_id_")})
                    if len(chunks) == 1:
                        # Make completed traces and their spans visible in one commit.
                        with writer.begin():
                            for trace, experiment, timestamp in (first_chunk, last_chunk):
                                writer.exec_driver_sql("""
                                    INSERT INTO trace_info
                                        (request_id, experiment_id, timestamp_ms, status, execution_time_ms)
                                    VALUES (%s, %s, %s, 'OK', 1)
                                """, (trace, experiment, timestamp))
                                writer.exec_driver_sql("""
                                    INSERT INTO spans (trace_id, experiment_id, span_id, name, status,
                                                       start_time_unix_nano, end_time_unix_nano, content)
                                    VALUES (%s, %s, '0000000000000001', 'root', 'OK', %s, %s, '{"name":"root"}')
                                """, (trace, experiment, timestamp * 1000000, (timestamp + 1) * 1000000))

                event.listen(reader, "after_cursor_execute", commit_after_first_select)
                try:
                    observed = candidate_rows(find_candidates(store, session, ids, CUTOFF, BATCH_SIZE))
                finally:
                    event.remove(reader, "after_cursor_execute", commit_after_first_select)

            self.assertEqual(chunks, [set(range(1, 1001)), {1001}])
            self.assertEqual(observed, [last_chunk, *before[:-1]])
            with store.ManagedSessionMaker() as session:
                next_batch = candidate_rows(find_candidates(store, session, ids, CUTOFF, BATCH_SIZE))
                self.assertEqual(next_batch, [first_chunk, last_chunk, *before[:-2]])
