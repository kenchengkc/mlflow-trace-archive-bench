-- Standalone psql reproduction for MLflow 3128eb46928fab08500124e9e0cde6791cf845aa.
-- Run with psql -X -d scratch -f repro.sql in a new connection.
-- Requires CREATE on the database. If the script fails after creating its
-- schema, the schema printed below may remain in the scratch database.
-- rows=100000, archived_pct=95, batch_size=100, experiments=1, 2, 3, 4, 5, 6, 7, 8
\set ON_ERROR_STOP on
SELECT 'mlflow_archive_repro_' || pg_backend_pid() AS repro_schema \gset
\echo Scratch schema: :repro_schema
CREATE SCHEMA :"repro_schema";
SET search_path TO :"repro_schema", pg_temp;
\set rows 100000
\set archived_pct 95
\set batch_size 100000

-- Reduced PostgreSQL schema for candidate selection. Types and key order follow
-- https://github.com/mlflow/mlflow/blob/3128eb46928fab08500124e9e0cde6791cf845aa/mlflow/store/tracking/dbmodels/models.py
-- The runner installs the archival-order index only for the indexed comparison.
BEGIN;

CREATE TABLE trace_info (
    request_id varchar(50) PRIMARY KEY,
    experiment_id integer NOT NULL,
    timestamp_ms bigint NOT NULL,
    status varchar(50) NOT NULL
);
CREATE INDEX index_trace_info_experiment_id_timestamp_ms
    ON trace_info (experiment_id, timestamp_ms);

CREATE TABLE trace_tags (
    request_id varchar(50) NOT NULL REFERENCES trace_info ON DELETE CASCADE,
    key varchar(250) NOT NULL,
    value varchar(8000),
    CONSTRAINT trace_tag_pk PRIMARY KEY (request_id, key)
);
CREATE INDEX index_trace_tags_request_id ON trace_tags (request_id);

CREATE TABLE spans (
    trace_id varchar(50) NOT NULL REFERENCES trace_info ON DELETE CASCADE,
    span_id varchar(50) NOT NULL,
    content text NOT NULL,
    CONSTRAINT spans_pk PRIMARY KEY (trace_id, span_id)
);

COMMIT;

-- Synthetic fixture. Eight traces share each timestamp; their IDs run in
-- reverse arrival order so an accidental ORDER BY request_id alone is visible.
BEGIN;
CREATE TEMP TABLE seed_input (rows bigint NOT NULL CHECK (rows > 0)) ON COMMIT DROP;
INSERT INTO seed_input VALUES (:'rows'::bigint);

CREATE TEMP TABLE seed_traces ON COMMIT DROP AS
SELECT n,
       'tr-' || lpad(to_hex(rows - n + 1), 32, '0') AS request_id,
       CASE WHEN n % 31 = 0 THEN 99
            WHEN n % 10 < 6 THEN 1
            ELSE 2 + ((n / 10) % 7)::integer END AS experiment_id,
       1700000000000 + (n - 1) / 8 - ((rows * 98 / 100) / 8) AS timestamp_ms,
       CASE WHEN n % 19 = 0 THEN 'IN_PROGRESS'
            WHEN n % 7 = 0 THEN 'ERROR'
            ELSE 'OK' END AS status
FROM seed_input CROSS JOIN LATERAL generate_series(1, rows) AS seq(n);

INSERT INTO trace_info (request_id, experiment_id, timestamp_ms, status)
SELECT request_id, experiment_id, timestamp_ms, status FROM seed_traces;

-- Some traces have no spans, some have only empty spans, and some have two.
-- The content is opaque to candidate selection; a nonempty span is sufficient.
INSERT INTO spans (trace_id, span_id, content)
SELECT request_id, '0000000000000001',
       CASE WHEN n % 11 = 0 THEN '' ELSE '{"name":"root"}' END
FROM seed_traces WHERE n % 13 <> 0
UNION ALL
SELECT request_id, '0000000000000002', '{"name":"child"}'
FROM seed_traces WHERE n % 5 = 0 AND n % 13 <> 0;

INSERT INTO trace_tags (request_id, key, value)
SELECT request_id, 'mlflow.trace.archivalFailure', 'MALFORMED_TRACE'
FROM seed_traces WHERE n % 17 = 0
UNION ALL
SELECT request_id, 'fixture.cohort', 'sample'
FROM seed_traces WHERE n % 4 = 0;

COMMIT;
ANALYZE trace_info;
ANALYZE spans;
ANALYZE trace_tags;

CREATE TEMP TABLE eligible AS -- One experiment chunk and a fixed retention cutoff, mirroring
-- _find_archivable_trace_candidates_for_experiments at MLflow 3128eb4.
-- https://github.com/mlflow/mlflow/issues/24895
SELECT t.request_id, t.experiment_id, t.timestamp_ms
FROM trace_info AS t
WHERE t.experiment_id IN (1, 2, 3, 4, 5, 6, 7, 8)
  AND t.timestamp_ms <= 1700000000000
  AND t.status <> 'IN_PROGRESS'
  AND NOT EXISTS (
      SELECT 1 FROM trace_tags AS location
      WHERE location.request_id = t.request_id
        AND location.key = 'mlflow.trace.spansLocation'
        AND location.value <> 'TRACKING_STORE'
  )
  AND NOT EXISTS (
      SELECT 1 FROM trace_tags AS failure
      WHERE failure.request_id = t.request_id
        AND failure.key = 'mlflow.trace.archivalFailure'
  )
  AND EXISTS (
      SELECT 1 FROM spans AS s
      WHERE s.trace_id = t.request_id AND s.content <> ''
  )
ORDER BY t.timestamp_ms, t.request_id
LIMIT :batch_size;
        CREATE TEMP TABLE archive_prefix AS
            SELECT * FROM eligible ORDER BY timestamp_ms, request_id
            LIMIT (SELECT count(*) * :archived_pct / 100 FROM eligible);
        -- The runner prepares pg_temp.archive_prefix from the oldest initially eligible
-- candidates for this fresh fixture. Rows include request_id and experiment_id.
-- Simulate the committed database state, without uploading artifacts.
-- https://github.com/mlflow/mlflow/blob/3128eb46928fab08500124e9e0cde6791cf845aa/mlflow/store/tracking/sqlalchemy_store.py
BEGIN;

UPDATE spans AS s SET content = ''
FROM pg_temp.archive_prefix AS archived
WHERE s.trace_id = archived.request_id;

INSERT INTO trace_tags (request_id, key, value)
SELECT archived.request_id, tag.key, tag.value
FROM pg_temp.archive_prefix AS archived
CROSS JOIN LATERAL (VALUES
    ('mlflow.trace.spansLocation', 'ARCHIVE_REPO'),
    ('mlflow.trace.archiveLocation', 'file:///synthetic/' || archived.experiment_id
        || '/traces/' || archived.request_id || '/artifacts')
) AS tag(key, value)
ON CONFLICT (request_id, key) DO UPDATE SET value = EXCLUDED.value;

COMMIT;
ANALYZE spans;
ANALYZE trace_tags;

        SELECT json_build_object('eligible_before', (SELECT count(*) FROM eligible),
                                 'archived', (SELECT count(*) FROM archive_prefix));

VACUUM (ANALYZE) trace_info;
VACUUM (ANALYZE) spans;
VACUUM (ANALYZE) trace_tags;

CREATE INDEX index_trace_info_timestamp_ms_request_id ON trace_info (timestamp_ms, request_id);

\echo Query plan for global
SELECT $candidate$
-- One experiment chunk and a fixed retention cutoff, mirroring
-- _find_archivable_trace_candidates_for_experiments at MLflow 3128eb4.
-- https://github.com/mlflow/mlflow/issues/24895
SELECT t.request_id, t.experiment_id, t.timestamp_ms
FROM trace_info AS t
WHERE t.experiment_id IN (1, 2, 3, 4, 5, 6, 7, 8)
  AND t.timestamp_ms <= 1700000000000
  AND t.status <> 'IN_PROGRESS'
  AND NOT EXISTS (
      SELECT 1 FROM trace_tags AS location
      WHERE location.request_id = t.request_id
        AND location.key = 'mlflow.trace.spansLocation'
        AND location.value <> 'TRACKING_STORE'
  )
  AND NOT EXISTS (
      SELECT 1 FROM trace_tags AS failure
      WHERE failure.request_id = t.request_id
        AND failure.key = 'mlflow.trace.archivalFailure'
  )
  AND EXISTS (
      SELECT 1 FROM spans AS s
      WHERE s.trace_id = t.request_id AND s.content <> ''
  )
ORDER BY t.timestamp_ms, t.request_id
LIMIT 100
$candidate$ AS candidate_sql \gset
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) :candidate_sql;

CREATE TEMP TABLE result_global (
    position bigint GENERATED ALWAYS AS IDENTITY,
    request_id varchar(50), experiment_id integer, timestamp_ms bigint
);
INSERT INTO pg_temp.result_global (request_id, experiment_id, timestamp_ms)
:candidate_sql;

\echo Query plan for per_experiment
SELECT $candidate$
-- Each experiment contributes at most K of the global first K rows. Use the
-- existing (experiment_id, timestamp_ms) index where the planner finds it useful.
SELECT local_top.request_id, local_top.experiment_id, local_top.timestamp_ms
FROM (SELECT DISTINCT unnest(ARRAY[1, 2, 3, 4, 5, 6, 7, 8]::integer[]) AS experiment_id) AS scope
CROSS JOIN LATERAL (
    SELECT t.request_id, t.experiment_id, t.timestamp_ms
    FROM trace_info AS t
    WHERE t.experiment_id = scope.experiment_id
      AND t.timestamp_ms <= 1700000000000
      AND t.status <> 'IN_PROGRESS'
      AND NOT EXISTS (
          SELECT 1 FROM trace_tags AS location
          WHERE location.request_id = t.request_id
            AND location.key = 'mlflow.trace.spansLocation'
            AND location.value <> 'TRACKING_STORE'
      )
      AND NOT EXISTS (
          SELECT 1 FROM trace_tags AS failure
          WHERE failure.request_id = t.request_id
            AND failure.key = 'mlflow.trace.archivalFailure'
      )
      AND EXISTS (
          SELECT 1 FROM spans AS s
          WHERE s.trace_id = t.request_id AND s.content <> ''
      )
    ORDER BY t.timestamp_ms, t.request_id
    LIMIT 100
) AS local_top
ORDER BY local_top.timestamp_ms, local_top.request_id
LIMIT 100
$candidate$ AS candidate_sql \gset
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) :candidate_sql;

CREATE TEMP TABLE result_per_experiment (
    position bigint GENERATED ALWAYS AS IDENTITY,
    request_id varchar(50), experiment_id integer, timestamp_ms bigint
);
INSERT INTO pg_temp.result_per_experiment (request_id, experiment_id, timestamp_ms)
:candidate_sql;

\echo Query plan for bounded_probe
SELECT $candidate$
-- Check the oldest two plausible rows per requested result, up to 4,096 rows.
-- If that prefix cannot fill the batch, compute the full eligible set.
WITH selected AS (
    SELECT unnest(ARRAY[1, 2, 3, 4, 5, 6, 7, 8]::integer[]) AS experiment_id
),
probe AS MATERIALIZED (
    SELECT t.request_id, t.experiment_id, t.timestamp_ms
    FROM trace_info AS t
    WHERE t.experiment_id = ANY(ARRAY(SELECT experiment_id FROM selected))
      AND t.timestamp_ms <= 1700000000000
      AND t.status <> 'IN_PROGRESS'
    ORDER BY t.timestamp_ms, t.request_id
    LIMIT (2 * LEAST(100::bigint, 2048))
),
probe_hits AS MATERIALIZED (
    -- Keep these checks as per-trace lookups for the bounded prefix.
    SELECT t.*
    FROM probe AS t
    WHERE EXISTS (
        SELECT 1 FROM spans AS s WHERE s.trace_id = t.request_id AND s.content <> ''
        OFFSET 0
    )
      AND NOT EXISTS (
          SELECT 1 FROM trace_tags AS tag
          WHERE tag.request_id = t.request_id
            AND (tag.key = 'mlflow.trace.archivalFailure'
                 OR (tag.key = 'mlflow.trace.spansLocation' AND tag.value <> 'TRACKING_STORE'))
          OFFSET 0
      )
    ORDER BY t.timestamp_ms, t.request_id
    LIMIT 100
),
decision AS (
    SELECT (SELECT count(*) FROM probe_hits) >= 100
        OR (SELECT count(*) FROM probe) < 2 * LEAST(100::bigint, 2048)
        AS prefix_suffices
),
full_hits AS MATERIALIZED (
    -- Separating eligibility from ordering lets the planner use set-oriented
    -- joins over the selected experiments when the prefix has few live traces.
    SELECT t.request_id, t.experiment_id, t.timestamp_ms
    FROM trace_info AS t
    WHERE t.experiment_id IN (SELECT experiment_id FROM selected)
      AND t.timestamp_ms <= 1700000000000
      AND t.status <> 'IN_PROGRESS'
      AND EXISTS (
          SELECT 1 FROM spans AS s WHERE s.trace_id = t.request_id AND s.content <> ''
      )
      AND NOT EXISTS (
          SELECT 1 FROM trace_tags AS tag
          WHERE tag.request_id = t.request_id
            AND (tag.key = 'mlflow.trace.archivalFailure'
                 OR (tag.key = 'mlflow.trace.spansLocation' AND tag.value <> 'TRACKING_STORE'))
      )
),
fallback AS (
    SELECT * FROM full_hits
    ORDER BY timestamp_ms, request_id
    LIMIT (SELECT CASE WHEN prefix_suffices THEN 0 ELSE 100 END FROM decision)
)
SELECT * FROM probe_hits WHERE (SELECT prefix_suffices FROM decision)
UNION ALL
SELECT * FROM fallback
ORDER BY timestamp_ms, request_id
$candidate$ AS candidate_sql \gset
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) :candidate_sql;

CREATE TEMP TABLE result_bounded_probe (
    position bigint GENERATED ALWAYS AS IDENTITY,
    request_id varchar(50), experiment_id integer, timestamp_ms bigint
);
INSERT INTO pg_temp.result_bounded_probe (request_id, experiment_id, timestamp_ms)
:candidate_sql;

-- Positions record the order of the actual returned rows, including ties.
CREATE TEMP TABLE repro_summary AS
WITH counts AS (
    SELECT
        (SELECT count(*) FROM eligible AS e
         WHERE e.experiment_id IN (1, 2, 3, 4, 5, 6, 7, 8) AND NOT EXISTS (
             SELECT 1 FROM archive_prefix AS a WHERE a.request_id = e.request_id
         )) AS eligible_in_scope,
        (SELECT count(*) FROM result_global) AS global_count,
        (SELECT count(*) FROM result_per_experiment) AS per_experiment_count,
        (SELECT count(*) FROM result_bounded_probe) AS bounded_probe_count
)
SELECT counts.*, least(eligible_in_scope, 100) AS expected_count,
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
DROP SCHEMA :"repro_schema" CASCADE;
