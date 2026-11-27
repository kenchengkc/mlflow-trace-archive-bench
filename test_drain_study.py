from dataclasses import replace
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from drain_fixture import seed_queue, verify_readback
from drain_measure import measure_drain
from local_postgres import local_database
from mlflow_driver import MLFLOW_VERSION, open_store
from test_driver_fixture import installed_mlflow


@unittest.skipUnless(installed_mlflow == MLFLOW_VERSION and find_spec("psycopg2"),
                     "requires the driver study's MLflow and psycopg2 dependencies")
class DrainStudyTest(unittest.TestCase):
    def test_ties_across_passes_partial_batch_and_archived_child_readback(self):
        with local_database() as (sql, uri), open_store(uri) as store, TemporaryDirectory() as tmp:
            expected = seed_queue(store, 8)
            archive = Path(tmp).resolve()
            passes = measure_drain(store, archive.as_uri(), expected)
            # The older timestamp covers suffixes 5–8; both timestamps span batch boundaries.
            batches = [[int(t.removeprefix("tr-d1a1"), 16) for t in row["selected_ids"]] for row in passes]
            self.assertEqual(batches, [[5, 6, 7], [8, 1, 2], [3, 4], []])
            self.assertEqual([row["archived_count"] for row in passes], [3, 3, 2, 0])
            for row in passes:
                self.assertGreater(row["selection_ms"], 0)
                self.assertLessEqual(row["selection_ms"], row["total_ms"])
            self.assertEqual(verify_readback(store, sql, expected, archive), {
                "traces_read_back": 8, "spans_read_back": 16,
                "database_span_payloads_cleared": 16, "archive_files_verified": 8,
            })
            # A correct root span alone must not conceal a different child payload.
            first_id = next(iter(expected))
            root, child = expected[first_id].spans
            expected[first_id] = replace(expected[first_id], spans=[root, dict(child, name="other-child")])
            with self.assertRaisesRegex(RuntimeError, "span readback"):
                verify_readback(store, sql, expected, archive)


if __name__ == "__main__":
    unittest.main()
