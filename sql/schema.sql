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
