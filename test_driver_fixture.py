import unittest
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec

from benchmark import prepare
from driver_fixture import prepare_driver_queue
from local_postgres import local_database, local_postgres
from matched_queues import inspect_queue
from mlflow_driver import MLFLOW_VERSION, open_store


try:
    installed_mlflow = version("mlflow")
except PackageNotFoundError:
    installed_mlflow = None


@unittest.skipUnless(installed_mlflow == MLFLOW_VERSION and find_spec("psycopg2"),
                     "requires the driver study's MLflow and psycopg2 dependencies")
class DriverFixtureTest(unittest.TestCase):
    def test_migrated_queues_match_reduced_fixtures(self):
        for pct, layout in ((0, "oldest_first"), (95, "oldest_first"), (95, "scattered")):
            with self.subTest(pct=pct, layout=layout):
                with local_postgres() as sql:
                    expected_counts = prepare(sql, 500, pct, layout=layout)
                    expected_queue = inspect_queue(sql, 5)
                with local_database() as (sql, uri), open_store(uri):
                    self.assertEqual(prepare_driver_queue(sql, 500, pct, layout=layout), expected_counts)
                    self.assertEqual(inspect_queue(sql, 5), expected_queue)
