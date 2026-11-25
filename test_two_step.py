import unittest
from importlib.util import find_spec

from driver_fixture import LOAD, STAGE
from local_postgres import local_database
from mlflow_driver import MLFLOW_VERSION, capture_statements, find_candidates, open_store
from test_candidates import FIXTURE
from test_driver_fixture import installed_mlflow


CUTOFF = 1700000000000
EXPERIMENTS = tuple(range(1, 9))
FIRST = ("first", 2, CUTOFF - 2)
TIE_A = ("tie-a", 8, CUTOFF - 1)
TIE_B = ("tie-b", 1, CUTOFF - 1)


@unittest.skipUnless(installed_mlflow == MLFLOW_VERSION and find_spec("psycopg2"),
                     "requires the driver study's MLflow and psycopg2 dependencies")
class TwoStepTest(unittest.TestCase):
    def test_writer_commits_between_probe_and_fallback(self):
        from sqlalchemy import event
        from two_step import PREFIX_SQL, snapshot_connection, two_step_candidates

        with local_database() as (sql, uri), open_store(uri) as store:
            sql(STAGE + FIXTURE + LOAD)
            # The initial six headers include FIRST and TIE_A. TIE_B is next
            # in archival order, beyond the prefix inside the same timestamp tie.
            before = [FIRST, TIE_A, TIE_B]
            after = [("still-running", 2, CUTOFF - 10), FIRST, TIE_B]

            def upstream_batch():
                with store.ManagedSessionMaker() as session:
                    rows = find_candidates(store, session, list(map(str, EXPERIMENTS)), CUTOFF, 3)
                    return [(row.trace_id, int(row.experiment_id), row.timestamp_ms) for row in rows]

            self.assertEqual(upstream_batch(), before)
            with store.ManagedSessionMaker() as session, store.engine.connect() as writer:
                reader = snapshot_connection(session)
                self.assertNotEqual(reader.connection.driver_connection.get_backend_pid(),
                                    writer.connection.driver_connection.get_backend_pid())
                calls = []

                def commit_after_probe(conn, cursor, statement, parameters, context, executemany):
                    calls.append(statement)
                    if statement == PREFIX_SQL:
                        # The reader's first SELECT has executed. Commit on a separate
                        # backend before returning control to the selector for its fallback.
                        with writer.begin():
                            writer.exec_driver_sql("UPDATE trace_info SET status = 'OK', execution_time_ms = 1 "
                                                   "WHERE request_id = 'still-running'")
                            writer.exec_driver_sql("UPDATE spans SET status = 'OK', "
                                                   "end_time_unix_nano = start_time_unix_nano + 1000000 "
                                                   "WHERE trace_id = 'still-running'")
                            writer.exec_driver_sql("UPDATE spans SET content = '' WHERE trace_id = 'tie-a'")
                            writer.exec_driver_sql("UPDATE trace_tags SET value = 'ARCHIVE_REPO' "
                                                   "WHERE request_id = 'tie-a' AND key = 'mlflow.trace.spansLocation'")
                            writer.exec_driver_sql("INSERT INTO trace_tags (request_id, key, value) VALUES "
                                                   "('tie-a', 'mlflow.trace.archiveLocation', 'file:///synthetic/tie-a')")

                event.listen(reader, "after_cursor_execute", commit_after_probe)
                try:
                    rows, used_fallback = two_step_candidates(reader, EXPERIMENTS, CUTOFF, 3)
                finally:
                    event.remove(reader, "after_cursor_execute", commit_after_probe)
                self.assertTrue(used_fallback)
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0], PREFIX_SQL)
                self.assertEqual(rows, before)
                self.assertEqual(reader.exec_driver_sql("SHOW transaction_isolation").scalar_one(), "repeatable read")
                self.assertEqual(reader.exec_driver_sql("SHOW transaction_read_only").scalar_one(), "on")

            self.assertEqual(upstream_batch(), after)
            with store.ManagedSessionMaker() as session:
                rows, used_fallback = two_step_candidates(snapshot_connection(session), EXPERIMENTS, CUTOFF, 3)
                self.assertTrue(used_fallback)
                self.assertEqual(rows, after)

    def test_query_count_and_order_for_full_short_and_empty_batches(self):
        from two_step import bounded_candidates, snapshot_connection, two_step_candidates

        with local_database() as (sql, uri), open_store(uri) as store:
            sql(STAGE + FIXTURE + LOAD)
            for experiments, limit, expected, calls in (
                ((2,), 1, [FIRST], 1),                  # Full batch from prefix.
                (EXPERIMENTS, 2, [FIRST, TIE_A], 2),    # No eligible row in a full prefix.
                ((8,), 3, [TIE_A], 1),                  # Exhausted short prefix.
                ((4,), 2, [], 1),                       # A header with no eligible span.
                ((999,), 2, [], 1),                     # No matching header.
                (EXPERIMENTS, 0, [], 0),
                ((), 2, [], 0),
            ):
                with self.subTest(experiments=experiments, limit=limit):
                    with capture_statements(store.engine) as sent, store.ManagedSessionMaker() as session:
                        result, fallback = two_step_candidates(snapshot_connection(session), experiments,
                                                               CUTOFF, limit)
                    self.assertEqual(result, expected)
                    self.assertEqual(fallback, calls == 2)
                    self.assertEqual(len(sent), calls)

            with store.ManagedSessionMaker() as session:
                connection = session.connection()
                with self.assertRaises(ValueError):
                    two_step_candidates(connection, EXPERIMENTS, CUTOFF, 3)
                with self.assertRaises(ValueError):
                    snapshot_connection(session)
            with store.ManagedSessionMaker() as session:
                connection = snapshot_connection(session)
                for selector in (two_step_candidates, bounded_candidates):
                    with self.subTest(selector=selector.__name__), self.assertRaises(ValueError):
                        selector(connection, EXPERIMENTS, None, 3)

    def test_batch_at_and_above_4096_header_cap(self):
        from two_step import snapshot_connection, two_step_candidates

        with local_database() as (sql, uri), open_store(uri) as store:
            sql(STAGE + """
                INSERT INTO pg_temp.trace_info
                SELECT 't-' || lpad(i::text, 4, '0'), 1, 1000, 'OK' FROM generate_series(4097, 1, -1) AS i;
                INSERT INTO pg_temp.spans SELECT request_id, 'root', '{}' FROM pg_temp.trace_info;
            """ + LOAD)
            ordered = [(f"t-{i:04}", 1, 1000) for i in range(1, 4098)]
            for limit, fallback in ((4096, False), (4097, True)):
                with self.subTest(limit=limit):
                    with capture_statements(store.engine) as sent, store.ManagedSessionMaker() as session:
                        rows, used_fallback = two_step_candidates(snapshot_connection(session), (1,), 1000, limit)
                    self.assertEqual(rows, ordered[:limit])
                    self.assertEqual(used_fallback, fallback)
                    self.assertEqual(len(sent), 2 if fallback else 1)
