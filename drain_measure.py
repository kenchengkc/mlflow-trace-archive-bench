"""Measure each real MLflow archival pass, including the first empty pass."""

from contextlib import ExitStack
from time import perf_counter_ns
from unittest.mock import patch


BATCH_SIZE = 3
RETENTION = "1d"
PHASES = ("span_load", "serialization", "artifact_write", "db_finalize")


def _time_phase(name, call, intervals):
    def timed(*args, **kwargs):
        start = perf_counter_ns()
        try:
            return call(*args, **kwargs)
        finally:
            intervals.append((name, start, perf_counter_ns()))
    return timed


def _install_phase_timers(stack, store, intervals, finalization_probe=None):
    from mlflow.store.artifact.local_artifact_repo import LocalArtifactRepository

    # MLflow constructs a new repository per trace. Patch the local backend only;
    # its real upload method stages the protobuf and copies it to the archive.
    for phase, owner, method in (
        ("span_load", store, "_load_trace_archival_data"),
        ("serialization", store, "_serialize_trace_archival_span_rows_to_pb"),
        ("artifact_write", LocalArtifactRepository, "upload_archived_trace_data_bytes"),
        ("db_finalize", store, "_finalize_archived_trace"),
    ):
        original = getattr(owner, method)
        if phase == "db_finalize" and finalization_probe is not None:
            original = finalization_probe.wrap_finalize(original)
        wrapped = _time_phase(phase, original, intervals)
        stack.enter_context(patch.object(owner, method, new=wrapped))


def _phase_row(intervals, count, selection_end, selection_ns, pass_end, total_ns):
    # This pinned path performs all four calls sequentially for each successful trace.
    if [name for name, _, _ in intervals] != list(PHASES) * count:
        raise RuntimeError("Unexpected archival phase order or call count")
    previous_end = selection_end
    elapsed = dict.fromkeys(PHASES, 0)
    calls = dict.fromkeys(PHASES, 0)
    for name, start, end in intervals:
        if not previous_end <= start <= end <= pass_end:
            raise RuntimeError("Archival phase intervals overlap or fall outside the pass")
        elapsed[name] += end - start
        calls[name] += 1
        previous_end = end
    elapsed["other"] = total_ns - selection_ns - sum(elapsed.values())
    if elapsed["other"] < 0:
        raise RuntimeError("Measured archival phases exceed the complete pass")
    return {"phase_ms": {key: ns / 1e6 for key, ns in elapsed.items()}, "phase_calls": calls}


def measure_drain(store, archive_uri: str, expected_ids, *, profile_phases=False,
                  profile_finalization=False, profile_orm=False, profile_queries=False,
                  profile_results=False, profile_result_detail=False, profile_timer_overhead=False,
                  profile_consumption=False, profile_tag_load=False, profile_tag_prep=False) -> list[dict]:
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
        ended = perf_counter_ns()
        # Capture the returned list by reference. Inspect its IDs outside the pass timer.
        plans.append((ended - started, ended, result[1]))
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
    intervals = []
    probe = None
    orm = None
    queries = None
    results = None
    consumption = None
    tag_load = None
    overhead = None
    profile_tag_load = profile_tag_load or profile_tag_prep
    profile_consumption = profile_consumption or profile_tag_load
    profile_result_detail = profile_result_detail or profile_timer_overhead or profile_consumption
    profile_results = profile_results or profile_result_detail
    profile_queries = profile_queries or profile_results
    profile_orm = profile_orm or profile_queries
    profile_finalization = profile_finalization or profile_orm
    profile_phases = profile_phases or profile_finalization
    with ExitStack() as stack:
        def install(timer):
            if overhead is not None:
                overhead.instrument(timer)
            timer.install(stack)

        stack.enter_context(patch.object(store, "_plan_trace_archival", new=timed_plan))
        stack.enter_context(patch.object(store, "_archive_trace_candidate", new=observe_archive))
        if profile_finalization:
            from finalize_probe import FinalizeProbe

            probe = FinalizeProbe(store.engine.dialect)
            if profile_timer_overhead:
                from timer_overhead import TimerOverhead

                overhead = TimerOverhead(probe)
            install(probe)
        if profile_orm:
            from orm_probe import OrmProbe

            orm = OrmProbe(probe)
            install(orm)
        if profile_queries:
            from query_probe import QueryProbe

            queries = QueryProbe(probe)
            install(queries)
        if profile_consumption:
            from consumption_probe import ConsumptionProbe

            if profile_tag_load:
                from tag_load_probe import TagLoadProbe

                tag_load = TagLoadProbe(probe, profile_preparation=profile_tag_prep)
                tag_load.install(stack)
            # ResultProbe must time the whole instrumented single-row consumer.
            # Overhead envelopes continue to surround only the original timers.
            consumption = ConsumptionProbe(probe, tag_load_probe=tag_load)
            consumption.install(stack)
        if profile_results:
            from result_probe import ResultProbe

            results = ResultProbe(probe, profile_detail=profile_result_detail)
            install(results)
        if overhead is not None:
            overhead.install_custom(stack)
        if profile_phases:
            _install_phase_timers(stack, store, intervals, probe)
        while True:
            plans.clear()
            successful.clear()
            intervals.clear()
            if probe is not None:
                probe.events.clear()
            if orm is not None:
                orm.events.clear()
            if queries is not None:
                queries.reset()
            if results is not None:
                results.events.clear()
            if consumption is not None:
                consumption.reset()
            if tag_load is not None:
                tag_load.reset()
            if overhead is not None:
                overhead.events.clear()
            started = perf_counter_ns()
            count = store.archive_traces(
                resolved_trace_archival_location=archive_uri,
                broader_retention=RETENTION,
                max_traces_per_pass=BATCH_SIZE,
            )
            ended = perf_counter_ns()
            total_ns = ended - started

            pass_number = len(rows) + 1
            if len(plans) != 1:
                raise RuntimeError(f"Pass {pass_number} called the selection planner {len(plans)} times")
            selection_ns, selection_end, candidates = plans[0]
            selected = [candidate.trace_id for candidate in candidates]
            expected = expected_ids[offset:offset + BATCH_SIZE]
            if selected != expected or successful != expected or count != len(expected):
                raise RuntimeError(
                    f"Pass {pass_number}: expected {expected}, selected {selected}, "
                    f"archived {successful}, store returned {count}"
                )
            row = {
                "pass_number": pass_number, "archived_count": count, "selected_ids": selected,
                "selection_ms": selection_ns / 1e6, "total_ms": total_ns / 1e6,
            }
            if profile_phases:
                row.update(_phase_row(intervals, count, selection_end, selection_ns, ended, total_ns))
            if probe is not None:
                detail = probe.summarize(
                    intervals, orm_events=None if orm is None else orm.events,
                    query_events=None if queries is None else queries.events,
                    result_events=None if results is None else results.events,
                    profile_result_detail=profile_result_detail,
                    overhead_events=None if overhead is None else overhead.events,
                    consumption_events=None if consumption is None else consumption.events,
                    tag_load_events=None if tag_load is None else tag_load.events,
                    tag_prep_events=tag_load.prep_events if profile_tag_prep else None,
                )
                row["finalization_ms"] = detail["ms"]
                row["finalization_calls"] = detail["calls"]
                if orm is not None:
                    row["orm_ms"] = detail["orm_ms"]
                    row["orm_calls"] = detail["orm_calls"]
                if queries is not None:
                    row["query_ms"] = detail["query_ms"]
                    row["query_calls"] = detail["query_calls"]
                    row["query_cache_counts"] = queries.cache_counts.copy()
                if results is not None:
                    row["result_ms"] = detail["result_ms"]
                    row["result_calls"] = detail["result_calls"]
                if profile_result_detail:
                    row["result_detail_ms"] = detail["result_detail_ms"]
                    row["result_detail_calls"] = detail["result_detail_calls"]
                if consumption is not None:
                    row["consumption_ms"] = detail["consumption_ms"]
                    row["consumption_calls"] = detail["consumption_calls"]
                    row["consumption_counts"] = consumption.counts.copy()
                    row["eager_tag_load_ms"] = detail["eager_tag_load_ms"]
                if tag_load is not None:
                    row["tag_load_ms"] = detail["tag_load_ms"]
                    row["tag_load_calls"] = detail["tag_load_calls"]
                    row["tag_load_counts"] = tag_load.counts.copy()
                if profile_tag_prep:
                    row["tag_prep_ms"] = detail["tag_prep_ms"]
                    row["tag_prep_calls"] = detail["tag_prep_calls"]
                    row["tag_prep_counts"] = tag_load.prep_counts.copy()
                if overhead is not None:
                    row["overhead_ms"] = detail["overhead_ms"]
                    row["overhead_calls"] = detail["overhead_calls"]
            rows.append(row)
            if count == 0:
                return rows
            offset += count
