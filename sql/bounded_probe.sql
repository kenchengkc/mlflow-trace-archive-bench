-- Check the oldest two plausible rows per requested result, up to 4,096 rows.
-- If that prefix cannot fill the batch, compute the full eligible set.
WITH selected AS (
    SELECT unnest(ARRAY[:experiment_ids]::integer[]) AS experiment_id
),
probe AS MATERIALIZED (
    SELECT t.request_id, t.experiment_id, t.timestamp_ms
    FROM trace_info AS t
    WHERE t.experiment_id = ANY(ARRAY(SELECT experiment_id FROM selected))
      AND t.timestamp_ms <= 1700000000000
      AND t.status <> 'IN_PROGRESS'
    ORDER BY t.timestamp_ms, t.request_id
    LIMIT (2 * LEAST(:batch_size::bigint, 2048))
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
    LIMIT :batch_size
),
decision AS (
    SELECT (SELECT count(*) FROM probe_hits) >= :batch_size
        OR (SELECT count(*) FROM probe) < 2 * LEAST(:batch_size::bigint, 2048)
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
    LIMIT (SELECT CASE WHEN prefix_suffices THEN 0 ELSE :batch_size END FROM decision)
)
SELECT * FROM probe_hits WHERE (SELECT prefix_suffices FROM decision)
UNION ALL
SELECT * FROM fallback
ORDER BY timestamp_ms, request_id
