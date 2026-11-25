-- One experiment chunk and a fixed retention cutoff, mirroring
-- _find_archivable_trace_candidates_for_experiments at MLflow 3128eb4.
-- https://github.com/mlflow/mlflow/issues/24895
SELECT t.request_id, t.experiment_id, t.timestamp_ms
FROM trace_info AS t
WHERE t.experiment_id IN (:experiment_ids)
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
