"""Write completed single-span traces through the pinned MLflow tracking store."""
from base64 import b64encode
import json

from chunk_fixture import CUTOFF


def write_trace(store, series: int, index: int) -> None:
    """Return after log_spans commits a new trace in experiment 1000 or 1001."""
    from mlflow.entities import Span
    from mlflow.tracing.constant import SpanAttributeKey

    # Each measurement window has its own ID namespace. Both query chunks
    # receive fresh writes, all safely newer than the fixed retention cutoff.
    trace_hex = f"f00d{series:012x}{index:016x}"
    timestamp_ms = CUTOFF + 1 + index
    span = Span.from_dict({
        "name": "ingestion-root",
        "trace_id": b64encode(bytes.fromhex(trace_hex)).decode("ascii"),
        "span_id": "AAAAAAAAAAE=",  # The eight-byte span ID 1, scoped to this trace.
        "parent_span_id": "",
        "attributes": {SpanAttributeKey.REQUEST_ID: json.dumps("tr-" + trace_hex)},
        "start_time_unix_nano": timestamp_ms * 1_000_000,
        "end_time_unix_nano": (timestamp_ms + 1) * 1_000_000,
        "status": {"code": "STATUS_CODE_OK"},
    })
    store.log_spans(str(1000 + index % 2), [span])


def verify_ingested(sql, series: int, count: int) -> dict:
    """Read back this window's committed headers and root spans from a separate session."""
    result = sql("""
        WITH prefix AS (
            SELECT 'tr-f00d' || lpad(to_hex(:series::bigint), 12, '0') AS id
        ), written AS (
            SELECT request_id, experiment_id, timestamp_ms, status, execution_time_ms
            FROM trace_info WHERE request_id LIKE (SELECT id || '%' FROM prefix)
        ), expected AS (
            SELECT id || lpad(to_hex(i), 16, '0') AS request_id, 1000 + i % 2 AS experiment_id,
                   :cutoff::bigint + 1 + i AS timestamp_ms
            FROM prefix, generate_series(0::bigint, :count::bigint - 1) AS i
        ), written_spans AS (
            SELECT s.* FROM spans s JOIN written w ON w.request_id = s.trace_id
        )
        SELECT (SELECT count(*) FROM written), (SELECT count(*) FROM written_spans), count(*)
        FROM expected e JOIN written w USING (request_id, experiment_id, timestamp_ms)
        JOIN written_spans s ON s.trace_id = w.request_id AND s.experiment_id = w.experiment_id
        WHERE w.status = 'OK' AND w.execution_time_ms = 1 AND s.status = 'OK'
          AND s.span_id = '0000000000000001' AND s.parent_span_id IS NULL AND s.content <> ''
          AND s.start_time_unix_nano = e.timestamp_ms * 1000000
          AND s.end_time_unix_nano = (e.timestamp_ms + 1) * 1000000;
    """, variables={"series": series, "count": count, "cutoff": CUTOFF})
    headers, spans, valid = map(int, result.split("|"))
    if (headers, spans, valid) != (count, count, count):
        raise RuntimeError(f"Expected {count} committed traces in series {series}, got {result}")
    return {"trace_headers": headers, "root_spans": spans}
