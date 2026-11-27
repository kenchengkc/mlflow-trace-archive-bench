"""Measure each real MLflow archival pass, including the first empty pass."""

from time import perf_counter_ns
from unittest.mock import patch


BATCH_SIZE = 3
RETENTION = "1d"


def measure_drain(store, archive_uri: str, expected_ids) -> list[dict]:
    """Check selected and successfully archived IDs against an independent ordered oracle."""
    expected_ids = list(expected_ids)
    if len(set(expected_ids)) != len(expected_ids):
        raise ValueError("Expected trace IDs must be unique")

    plan = store._plan_trace_archival
    archive_candidate = store._archive_trace_candidate
    plans = []
    successful = []

    def timed_plan(**kwargs):
        started = perf_counter_ns()
        result = plan(**kwargs)
        elapsed = perf_counter_ns() - started
        # Capture the returned list by reference. Inspect its IDs outside the pass timer.
        plans.append((elapsed, result[1]))
        return result

    def observe_archive(*, trace_id, resolved_trace_archival_location):
        archived = archive_candidate(
            trace_id=trace_id, resolved_trace_archival_location=resolved_trace_archival_location,
        )
        if archived:
            successful.append(trace_id)
        return archived

    rows = []
    offset = 0
    with (
        patch.object(store, "_plan_trace_archival", new=timed_plan),
        patch.object(store, "_archive_trace_candidate", new=observe_archive),
    ):
        while True:
            plans.clear()
            successful.clear()
            started = perf_counter_ns()
            count = store.archive_traces(
                resolved_trace_archival_location=archive_uri,
                broader_retention=RETENTION,
                max_traces_per_pass=BATCH_SIZE,
            )
            total_ns = perf_counter_ns() - started

            pass_number = len(rows) + 1
            if len(plans) != 1:
                raise RuntimeError(f"Pass {pass_number} called the selection planner {len(plans)} times")
            selection_ns, candidates = plans[0]
            selected = [candidate.trace_id for candidate in candidates]
            expected = expected_ids[offset:offset + BATCH_SIZE]
            if selected != expected or successful != expected or count != len(expected):
                raise RuntimeError(
                    f"Pass {pass_number}: expected {expected}, selected {selected}, "
                    f"archived {successful}, store returned {count}"
                )
            rows.append({
                "pass_number": pass_number, "archived_count": count, "selected_ids": selected,
                "selection_ms": selection_ns / 1e6, "total_ms": total_ns / 1e6,
            })
            if count == 0:
                return rows
            offset += count
