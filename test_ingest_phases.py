from importlib.util import find_spec
import unittest
from unittest.mock import patch

import chunk_ingest
from ingest_phases import PHASES, WriterPhases, capture_writer_phases
from local_postgres import local_database
from mlflow_driver import MLFLOW_VERSION, open_store
from test_driver_fixture import installed_mlflow


class PhaseBoundariesTest(unittest.TestCase):
    def test_records_only_successful_nonoverlapping_traces(self):
        recorder = WriterPhases()
        recorder.measure("commit", int, "1")  # Untimed warmup.
        with patch("ingest_phases.perf_counter_ns", side_effect=[20, 27, 34, 49]):
            with recorder.trace():
                self.assertEqual(recorder.measure("commit", int, "12"), 12)
                recorder.measure("commit", int, "1")
        row, = recorder.samples
        self.assertEqual(row["calls"]["commit"], 2)
        self.assertEqual(sum(row["duration_ns"].values()), 22)
        with self.assertRaisesRegex(RuntimeError, "Overlapping phases"), recorder.trace():
            recorder.measure("build_span", recorder.measure, "commit", int, 1)
        self.assertEqual(recorder.samples, [row])


@unittest.skipUnless(installed_mlflow == MLFLOW_VERSION and find_spec("psycopg2"),
                     "requires the driver study's MLflow and psycopg2 dependencies")
class WriterInstrumentationTest(unittest.TestCase):
    def test_times_the_live_store_and_restores_original_functions(self):
        from mlflow.store.tracking import sqlalchemy_store

        with local_database() as (sql, uri), open_store(uri) as store:
            sql("""INSERT INTO experiments (experiment_id, name, artifact_location, lifecycle_stage)
                   VALUES (1000, 'phases', 'file:///synthetic/1000', 'active')""")
            targets = [
                (chunk_ingest, "build_span"), (sqlalchemy_store, "translate_span_when_storing"),
                (sqlalchemy_store, "TraceJSONEncoder"),
                *((store.engine.dialect, name) for name in
                  ("do_commit", "do_execute", "do_execute_no_params", "do_executemany")),
            ]
            originals = [getattr(owner, name) for owner, name in targets]
            with self.assertRaisesRegex(ValueError, "end trial"), capture_writer_phases(store.engine) as recorder:
                chunk_ingest.write_trace(store, 3, 0)  # Warmup outside measured calls.
                for series, size in ((1, 0), (2, 65536)):
                    with recorder.trace():
                        chunk_ingest.write_trace(store, series, 0, chunk_ingest.payload_text(series, 0, size))
                    stored = chunk_ingest.verify_ingested(sql, series, 1, payload_bytes=size)
                    self.assertEqual(stored["payload"]["attribute_bytes_per_span"], size)
                raise ValueError("end trial")
            self.assertEqual([getattr(owner, name) for owner, name in targets], originals)
            self.assertEqual(len(recorder.samples), 2)
            for row in recorder.samples:
                self.assertEqual(row["calls"], {
                    "build_span": 1, "translate_span": 1, "encode_content": 1,
                    "span_write": 1, "other_write": 5, "other_sql": 4, "commit": 2,
                })
                self.assertTrue(all(row["duration_ns"][phase] > 0 for phase in PHASES))
