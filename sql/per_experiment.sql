-- Each experiment contributes at most K of the global first K rows. Use the
-- existing (experiment_id, timestamp_ms) index where the planner finds it useful.
SELECT local_top.request_id, local_top.experiment_id, local_top.timestamp_ms
FROM (SELECT DISTINCT unnest(ARRAY[:experiment_ids]::integer[]) AS experiment_id) AS scope
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
    LIMIT :batch_size
) AS local_top
ORDER BY local_top.timestamp_ms, local_top.request_id
LIMIT :batch_size
