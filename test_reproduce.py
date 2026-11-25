import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from local_postgres import local_postgres
from reproduce import export_sql
from study import strategy_queries


class ReproductionTest(unittest.TestCase):
    def test_export_runs_standalone_and_leaves_existing_tables_untouched(self):
        exporter = Path(__file__).with_name("reproduce.py").resolve()
        with TemporaryDirectory() as tmp, local_postgres() as sql:
            report = Path(tmp) / "repro.sql"
            with report.open("w") as output:
                subprocess.run([
                    sys.executable, str(exporter), "--rows", "128", "--archived-pct", "95",
                    "--batch-size", "2", "--scope", "1,2,3,4,5,6,7,8",
                ], cwd=tmp, stdout=output, check=True)
            sql("CREATE TABLE public.trace_info (sentinel text); "
                "INSERT INTO public.trace_info VALUES ('still here');")
            result = sql(f"\\i '{report}'\n")
            self.assertEqual(result.splitlines()[-1], "5|2|2|2|2|t")
            self.assertEqual(result.count("Execution Time:"), 3)
            self.assertIn("Buffers: shared", result)
            self.assertEqual(sql("TABLE public.trace_info;"), "still here")
            self.assertEqual(sql("SELECT count(*) FROM pg_namespace "
                                 "WHERE nspname ~ '^mlflow_archive_repro_[0-9]+$';"), "0")

    def test_equal_counts_with_different_order_fail(self):
        scope = (1, 2, 3)
        queries = strategy_queries(scope)
        queries["bounded_probe"] = (
            f"SELECT * FROM ({queries['bounded_probe']}) AS same_rows "
            "ORDER BY timestamp_ms DESC, request_id DESC"
        )
        with patch("reproduce.strategy_queries", return_value=queries):
            script = export_sql(128, 0, 4, scope)
        with local_postgres() as sql:
            with self.assertRaisesRegex(RuntimeError, "Candidate counts or ordered results differ"):
                sql("\\set ON_ERROR_STOP off\n" + script)

    def test_schema_collision_stops_before_fixture_creation(self):
        with local_postgres() as sql:
            setup = r"""
SELECT 'mlflow_archive_repro_' || pg_backend_pid() AS other_schema \gset
CREATE SCHEMA :"other_schema";
CREATE TABLE :"other_schema".sentinel (marker integer);
"""
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                sql(setup + export_sql(8, 0, 1, (1,)))
            self.assertEqual(sql("SELECT relname FROM pg_class AS c JOIN pg_namespace AS n "
                                 "ON n.oid = c.relnamespace WHERE n.nspname ~ '^mlflow_archive_repro_[0-9]+$';"),
                             "sentinel")


if __name__ == "__main__":
    unittest.main()
