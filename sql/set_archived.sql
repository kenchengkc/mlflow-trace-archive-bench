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
