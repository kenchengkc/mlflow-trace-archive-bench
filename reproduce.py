"""Write a standalone psql reproduction of one trace archival benchmark case to stdout."""
import argparse

from benchmark import ALL_EXPERIMENTS, CREATE_INDEX, MLFLOW_REVISION, SQL_DIR, archive_sql, positive_int
from study import parse_scope, strategy_queries


def export_sql(rows: int, archived_pct: int, batch_size: int, scope: tuple[int, ...]) -> str:
    if rows < 1 or batch_size < 1 or not 0 <= archived_pct <= 100:
        raise ValueError("rows and batch size must be positive; archived percent must be 0 through 100")
    queries = strategy_queries(scope)
    experiment_ids = ", ".join(map(str, scope))
    script = [
        f"""-- Standalone psql reproduction for MLflow {MLFLOW_REVISION}.
-- Run with psql -X -d scratch -f repro.sql in a new connection.
-- Requires CREATE on the database. If the script fails after creating its
-- schema, the schema printed below may remain in the scratch database.
-- rows={rows}, archived_pct={archived_pct}, batch_size={batch_size}, experiments={experiment_ids}
\\set ON_ERROR_STOP on
SELECT 'mlflow_archive_repro_' || pg_backend_pid() AS repro_schema \\gset
\\echo Scratch schema: :repro_schema
CREATE SCHEMA :"repro_schema";
SET search_path TO :"repro_schema", pg_temp;
\\set rows {rows}
\\set archived_pct {archived_pct}
\\set batch_size {rows}""",
        (SQL_DIR / "schema.sql").read_text(),
        (SQL_DIR / "seed.sql").read_text(),
        archive_sql(),
        "VACUUM (ANALYZE) trace_info;\nVACUUM (ANALYZE) spans;\nVACUUM (ANALYZE) trace_tags;",
        CREATE_INDEX,
    ]
    for name, query in queries.items():
        # Store the expanded query once. EXPLAIN and result capture both execute
        # these exact bytes, including the top-level ORDER BY.
        query = query.replace(":batch_size", str(batch_size))
        script.append(f"""\\echo Query plan for {name}
SELECT $candidate$
{query}
$candidate$ AS candidate_sql \\gset
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) :candidate_sql;

CREATE TEMP TABLE result_{name} (
    position bigint GENERATED ALWAYS AS IDENTITY,
    request_id varchar(50), experiment_id integer, timestamp_ms bigint
);
INSERT INTO pg_temp.result_{name} (request_id, experiment_id, timestamp_ms)
:candidate_sql;""")
    script.append(f"""-- Positions record the order of the actual returned rows, including ties.
CREATE TEMP TABLE repro_summary AS
WITH counts AS (
    SELECT
        (SELECT count(*) FROM eligible AS e
         WHERE e.experiment_id IN ({experiment_ids}) AND NOT EXISTS (
             SELECT 1 FROM archive_prefix AS a WHERE a.request_id = e.request_id
         )) AS eligible_in_scope,
        (SELECT count(*) FROM result_global) AS global_count,
        (SELECT count(*) FROM result_per_experiment) AS per_experiment_count,
        (SELECT count(*) FROM result_bounded_probe) AS bounded_probe_count
)
SELECT counts.*, least(eligible_in_scope, {batch_size}) AS expected_count,
       global_count = per_experiment_count AND global_count = bounded_probe_count
       AND NOT EXISTS (TABLE result_global EXCEPT TABLE result_per_experiment)
       AND NOT EXISTS (TABLE result_global EXCEPT TABLE result_bounded_probe)
       AS ordered_results_match
FROM counts;

TABLE pg_temp.repro_summary;
DO $verify$
BEGIN
    IF NOT (SELECT ordered_results_match AND global_count = expected_count FROM pg_temp.repro_summary) THEN
        RAISE EXCEPTION 'Candidate counts or ordered results differ; inspect repro_summary above';
    END IF;
END
$verify$;
RESET search_path;
DROP SCHEMA :"repro_schema" CASCADE;""")
    return "\n\n".join(part.strip() for part in script) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=positive_int, default=10000)
    parser.add_argument("--archived-pct", type=int, default=95)
    parser.add_argument("--batch-size", type=positive_int, default=100)
    parser.add_argument("--scope", type=parse_scope, default=ALL_EXPERIMENTS)
    args = parser.parse_args()
    if not 0 <= args.archived_pct <= 100:
        parser.error("--archived-pct must be between 0 and 100 inclusive")
    print(export_sql(args.rows, args.archived_pct, args.batch_size, args.scope), end="")


if __name__ == "__main__":
    main()
