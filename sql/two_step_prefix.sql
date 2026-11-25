-- Return eligible rows from the prefix plus its size, even when no row qualifies.
WITH selected AS (
    SELECT unnest(%(experiment_ids)s::integer[]) AS experiment_id
), probe AS MATERIALIZED (
    SELECT t.request_id, t.experiment_id, t.timestamp_ms
    FROM trace_info AS t
    WHERE t.experiment_id = ANY(ARRAY(SELECT experiment_id FROM selected))
      AND t.timestamp_ms <= %(cutoff)s
      AND t.status <> 'IN_PROGRESS'
    ORDER BY t.timestamp_ms, t.request_id
    LIMIT %(probe_limit)s
), hits AS (
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
    LIMIT %(batch_size)s
)
SELECT hits.request_id, hits.experiment_id, hits.timestamp_ms, counts.headers
FROM (SELECT count(*) AS headers FROM probe) AS counts
LEFT JOIN hits ON true
ORDER BY hits.timestamp_ms, hits.request_id
