"""Seed complete traces and verify their archived spans through MLflow's reader."""
from base64 import b64encode
from dataclasses import dataclass
import json
from pathlib import Path

from chunk_fixture import CUTOFF
from local_postgres import Sql


@dataclass(frozen=True)
class ExpectedTrace:
    experiment_id: str
    timestamp_ms: int
    spans: list[dict]


def seed_queue(store, trace_count: int) -> dict[str, ExpectedTrace]:
    """Return the input oracle in archival order after logging traces in reverse order."""
    from mlflow.entities import Span
    from mlflow.tracing.constant import SpanAttributeKey

    if trace_count < 1:
        raise ValueError("trace_count must be positive")
    experiments = [store.create_experiment(f"drain-{i}") for i in range(3)]
    traces = {}
    for index in range(trace_count):
        # IDs decrease as timestamps increase. Each four-way tie straddles a
        # three-trace batch boundary and includes traces from several experiments.
        trace_hex = f"d1a1{trace_count - index:028x}"
        trace_id = "tr-" + trace_hex
        timestamp_ms = CUTOFF + index // 4
        timestamp_ns = timestamp_ms * 1_000_000
        spans = []
        for number, name in enumerate(("root", "child"), start=1):
            span = Span.from_dict({
                "trace_id": b64encode(bytes.fromhex(trace_hex)).decode("ascii"),
                "span_id": b64encode(number.to_bytes(8, "big")).decode("ascii"),
                "parent_span_id": None if number == 1 else spans[0]["span_id"],
                "name": name,
                "start_time_unix_nano": timestamp_ns + (number - 1) * 100_000,
                "end_time_unix_nano": timestamp_ns + 1_000_000 - (number - 1) * 100_000,
                "attributes": {
                    SpanAttributeKey.REQUEST_ID: json.dumps(trace_id),
                    "benchmark.payload": json.dumps(f"{index}/{name}"),
                },
                "status": {"code": "STATUS_CODE_OK"},
            })
            spans.append(span.to_dict())
        traces[trace_id] = ExpectedTrace(experiments[index % 3], timestamp_ms, spans)

    ordered = dict(sorted(traces.items(), key=lambda item: (item[1].timestamp_ms, item[0])))
    for trace in reversed(ordered.values()):
        store.log_spans(trace.experiment_id, [Span.from_dict(doc) for doc in trace.spans])
    return ordered


def verify_readback(store, sql: Sql, expected: dict[str, ExpectedTrace], archive_root: Path) -> dict:
    """Check the DB payloads were cleared and every archived span matches its input."""
    from mlflow.tracing.constant import SpansLocation, TraceTagKey
    from mlflow.tracing.otel.otel_archival import TRACE_ARCHIVAL_FILENAME

    span_count = sum(len(trace.spans) for trace in expected.values())
    cleared = sql("SELECT count(*), count(*) FILTER (WHERE content = '') FROM spans")
    if cleared != f"{span_count}|{span_count}":
        raise RuntimeError(f"Expected {span_count} cleared spans, observed total|cleared={cleared}")
    for trace_id, original in expected.items():
        trace = store.get_trace(trace_id)
        artifact = archive_root / original.experiment_id / "traces" / trace_id / "artifacts"
        if (trace.info.trace_id != trace_id
                or trace.info.experiment_id != original.experiment_id
                or trace.info.request_time != original.timestamp_ms
                or trace.info.tags.get(TraceTagKey.SPANS_LOCATION) != SpansLocation.ARCHIVE_REPO.value
                or trace.info.tags.get(TraceTagKey.ARCHIVE_LOCATION) != artifact.as_uri()
                or not (artifact / TRACE_ARCHIVAL_FILENAME).is_file()):
            raise RuntimeError(f"Archived metadata or artifact does not match input trace {trace_id}")
        actual_spans = sorted((span.to_dict() for span in trace.data.spans), key=lambda doc: doc["span_id"])
        if actual_spans != original.spans:
            raise RuntimeError(f"Archived span readback differs from the input for {trace_id}")
    return {
        "traces_read_back": len(expected),
        "spans_read_back": span_count,
        "database_span_payloads_cleared": span_count,
        "archive_files_verified": len(expected),
    }
