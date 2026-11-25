"""Load the benchmark's deterministic trace population into an initialized MLflow database."""

from benchmark import SQL_DIR, archive_fixture
from local_postgres import Sql


# The seed can write its usual three columns per span. The temporary tables are
# visible only to this setup connection and disappear before archival and timing.
STAGE = """
CREATE TEMP TABLE trace_info AS SELECT request_id, experiment_id, timestamp_ms, status
    FROM public.trace_info WITH NO DATA;
CREATE TEMP TABLE spans AS SELECT trace_id, span_id, content FROM public.spans WITH NO DATA;
CREATE TEMP TABLE trace_tags AS SELECT request_id, key, value FROM public.trace_tags WITH NO DATA;
"""

LOAD = """
BEGIN;
INSERT INTO public.experiments (
    experiment_id, name, artifact_location, lifecycle_stage, creation_time, last_update_time
)
SELECT experiment_id, 'fixture-' || experiment_id, 'file:///synthetic/' || experiment_id,
       'active', min(timestamp_ms), max(timestamp_ms)
FROM pg_temp.trace_info GROUP BY experiment_id;
SELECT setval(pg_get_serial_sequence('public.experiments', 'experiment_id'),
              (SELECT max(experiment_id) FROM public.experiments));

INSERT INTO public.trace_info (request_id, experiment_id, timestamp_ms, status, execution_time_ms)
SELECT request_id, experiment_id, timestamp_ms, status,
       CASE WHEN status <> 'IN_PROGRESS' THEN 1 END
FROM pg_temp.trace_info ORDER BY request_id DESC;

INSERT INTO public.spans (
    trace_id, span_id, experiment_id, parent_span_id, name, status,
    start_time_unix_nano, end_time_unix_nano, content
)
SELECT s.trace_id, s.span_id, t.experiment_id,
       CASE WHEN s.span_id = '0000000000000002' THEN '0000000000000001' END,
       CASE WHEN s.span_id = '0000000000000002' THEN 'child' ELSE 'root' END,
       CASE WHEN t.status = 'IN_PROGRESS' THEN 'UNSET' ELSE t.status END,
       t.timestamp_ms * 1000000,
       CASE WHEN t.status <> 'IN_PROGRESS' THEN (t.timestamp_ms + 1) * 1000000 END,
       s.content
FROM pg_temp.spans AS s JOIN pg_temp.trace_info AS t ON t.request_id = s.trace_id
ORDER BY s.span_id, s.trace_id DESC;

INSERT INTO public.trace_tags (request_id, key, value)
SELECT request_id, key, value FROM pg_temp.trace_tags;
COMMIT;
ANALYZE public.experiments;
ANALYZE public.trace_info;
ANALYZE public.spans;
ANALYZE public.trace_tags;
"""


def prepare_driver_queue(sql: Sql, rows: int, archived_pct: int, *,
                         layout: str = "oldest_first", seed: int = 0) -> dict:
    """Seed fresh, migrated MLflow tables, keeping their indexes and constraints."""
    sql(STAGE + (SQL_DIR / "seed.sql").read_text() + LOAD, variables={"rows": rows})
    return archive_fixture(sql, rows, archived_pct, layout=layout, seed=seed)
