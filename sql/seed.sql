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
