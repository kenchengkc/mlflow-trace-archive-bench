-- Replace the oldest-first selection with a deterministic sample that archives
-- the same number of traces per experiment and the same number of span rows
-- within each experiment. Both layouts then use the same archival mutations.
CREATE TEMP TABLE archive_prefix AS
WITH span_counts AS (
    SELECT trace_id, count(*) AS span_count
    FROM spans
    GROUP BY trace_id
), ranked AS (
    SELECT e.*,
           count(p.request_id) OVER (
               PARTITION BY e.experiment_id, s.span_count
           ) AS archive_quota,
           row_number() OVER (
               PARTITION BY e.experiment_id, s.span_count
               ORDER BY md5(:'seed' || ':' || e.request_id) COLLATE "C", e.request_id
           ) AS sample_position
    FROM eligible AS e
    JOIN span_counts AS s ON s.trace_id = e.request_id
    LEFT JOIN oldest_prefix AS p ON p.request_id = e.request_id
)
SELECT request_id, experiment_id, timestamp_ms
FROM ranked
WHERE sample_position <= archive_quota
ORDER BY timestamp_ms, request_id;
