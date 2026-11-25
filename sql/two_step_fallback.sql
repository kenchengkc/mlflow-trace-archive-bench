-- Filter the entire scope before ordering, as in the single-statement fallback.
WITH selected AS (
    SELECT unnest(%(experiment_ids)s::integer[]) AS experiment_id
), full_hits AS MATERIALIZED (
    SELECT t.request_id, t.experiment_id, t.timestamp_ms
    FROM trace_info AS t
    WHERE t.experiment_id IN (SELECT experiment_id FROM selected)
      AND t.timestamp_ms <= %(cutoff)s
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
)
SELECT request_id, experiment_id, timestamp_ms
FROM full_hits
ORDER BY timestamp_ms, request_id
LIMIT %(batch_size)s
