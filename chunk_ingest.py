"""Write completed single-span traces through the pinned MLflow tracking store."""
from base64 import b64encode
from hashlib import sha256, shake_256
import json

from chunk_fixture import CUTOFF


def payload_text(series: int, index: int, payload_bytes: int) -> str | None:
    """Generate deterministic ASCII attribute data before the writer timer starts."""
    if payload_bytes < 0:
        raise ValueError("payload_bytes must be nonnegative")
    if payload_bytes == 0:
        return None
    key = f"mlflow-trace-archive-bench/{series}/{index}".encode("ascii")
    data = shake_256(key).digest((3 * payload_bytes + 3) // 4)
    return b64encode(data)[:payload_bytes].decode("ascii")


def write_trace(store, series: int, index: int, payload: str | None = None) -> None:
    """Return after log_spans commits a new trace in experiment 1000 or 1001."""
    from mlflow.entities import Span
    from mlflow.tracing.constant import SpanAttributeKey

    # Each measurement window has its own ID namespace. Both query chunks
    # receive fresh writes, all safely newer than the fixed retention cutoff.
    trace_hex = f"f00d{series:012x}{index:016x}"
    timestamp_ms = CUTOFF + 1 + index
    attributes = {SpanAttributeKey.REQUEST_ID: json.dumps("tr-" + trace_hex)}
    if payload is not None:
        attributes["benchmark.payload"] = json.dumps(payload)
    span = Span.from_dict({
        "name": "ingestion-root",
        "trace_id": b64encode(bytes.fromhex(trace_hex)).decode("ascii"),
        "span_id": "AAAAAAAAAAE=",  # The eight-byte span ID 1, scoped to this trace.
        "parent_span_id": "",
        "attributes": attributes,
        "start_time_unix_nano": timestamp_ms * 1_000_000,
        "end_time_unix_nano": (timestamp_ms + 1) * 1_000_000,
        "status": {"code": "STATUS_CODE_OK"},
    })
    store.log_spans(str(1000 + index % 2), [span])


def verify_ingested(sql, series: int, count: int, *, payload_bytes: int | None = None) -> dict:
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
    result = {"trace_headers": headers, "root_spans": spans}
    if payload_bytes is not None:
        result["payload"] = _verify_payloads(sql, series, count, payload_bytes)
    return result


def _verify_payloads(sql, series: int, count: int, payload_bytes: int) -> dict:
    # MLflow serializes each span attribute as a JSON-encoded string inside
    # spans.content. Decode it once more to check the original input bytes.
    rows = sql("""
        WITH docs AS (
            SELECT trace_id, content::jsonb AS doc, octet_length(content) AS logical_bytes,
                   pg_column_size(content) AS stored_bytes
            FROM spans
            WHERE trace_id LIKE 'tr-f00d' || lpad(to_hex(:series::bigint), 12, '0') || '%'
        ), decoded AS (
            SELECT *, (doc #>> '{attributes,benchmark.payload}')::jsonb #>> '{}' AS payload
            FROM docs
        )
        SELECT logical_bytes, stored_bytes, (doc->'attributes') ? 'benchmark.payload',
               octet_length(payload), encode(sha256(convert_to(payload, 'UTF8')), 'hex'),
               encode(sha256(convert_to((doc #- '{attributes,benchmark.payload}')::text, 'UTF8')), 'hex')
        FROM decoded ORDER BY trace_id;
    """, variables={"series": series}).splitlines()
    if len(rows) != count:
        raise RuntimeError(f"Expected {count} stored spans for payload verification, got {len(rows)}")
    logical_sizes, stored_sizes = [], []
    base_content = sha256()
    for index, row in enumerate(rows):
        logical, stored, present, size, digest, base_digest = row.split("|")
        expected = payload_text(series, index, payload_bytes)
        if expected is None:
            matches = (present, size, digest) == ("f", "", "")
        else:
            matches = (present, size, digest) == ("t", str(payload_bytes), sha256(expected.encode("ascii")).hexdigest())
        if not matches:
            raise RuntimeError(f"Stored payload differs in series {series}, trace index {index}")
        logical_sizes.append(int(logical))
        stored_sizes.append(int(stored))
        base_content.update(bytes.fromhex(base_digest))
    return {
        "attribute_bytes_per_span": payload_bytes, "base_content_sha256": base_content.hexdigest(),
        "logical_bytes_min": min(logical_sizes, default=None), "logical_bytes_max": max(logical_sizes, default=None),
        "logical_bytes_sum": sum(logical_sizes),
        "stored_bytes_min": min(stored_sizes, default=None), "stored_bytes_max": max(stored_sizes, default=None),
        "stored_bytes_sum": sum(stored_sizes),
    }
