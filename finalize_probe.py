"""Attribute finalization wall time to nested MLflow, SQLAlchemy and psycopg2 operations."""

from itertools import chain, pairwise
from time import perf_counter_ns
from unittest.mock import patch


FINALIZE_OPERATIONS = (
    "lock_trace", "read_trace_tags", "check_span_content", "clear_span_content",
    "update_tag", "lookup_tag", "insert_tag", "delete_tag", "other_sql", "commit", "rollback",
)
ORM_OPERATIONS = ("fetch", "objects", "session")
QUERY_OPERATIONS = ("construction", "compile_cache", "compilation", "dispatch")
RESULT_OPERATIONS = ("eligibility", "results")
RESULT_DETAIL_OPERATIONS = ("setup", "filtering", "consumption")
CONSUMPTION_OPERATIONS = ("iteration", "uniqueness", "eager_tags")


def _split_ns(windows, events, excluded, operations):
    """Partition time outside excluded intervals, giving nested calls priority."""
    calls = dict.fromkeys(operations, 0)
    elapsed = dict.fromkeys((*operations, "other"), 0)
    previous_end = None
    for start, end in windows:
        if end < start or (previous_end is not None and start < previous_end):
            raise RuntimeError("Finalization windows overlap or have negative duration")
        previous_end = end
    for interval in chain(excluded, events):
        start, end = interval[:2]
        if not any(left <= start <= end <= right for left, right in windows):
            raise RuntimeError("Recorded call falls outside a finalization window")

    # Earlier timers keep their time even when a new hook runs inside one.
    # Merge their intervals first so e.g. a driver call inside a flush is excluded once.
    covered = []
    for start, end in sorted(excluded):
        if covered and start <= covered[-1][1]:
            covered[-1] = (covered[-1][0], max(end, covered[-1][1]))
        else:
            covered.append((start, end))
    available = sum(end - start for start, end in windows) - sum(end - start for start, end in covered)
    elapsed["other"] = available
    parents = []
    # The probe records on return. For equal start and end timestamps, visit
    # the last recorded (outermost) scope first.
    for start, end, operation in sorted(reversed(events), key=lambda event: (event[0], -event[1])):
        calls[operation] += 1
        if start == end:
            continue
        while parents and parents[-1][0] <= start:
            parents.pop()
        if parents and end > parents[-1][0]:
            raise RuntimeError("Measured intervals overlap without nesting")
        # Transfer inclusive, unassigned time from the parent to the child.
        # Each deeper child will receive its own share in turn.
        excluded_ns = sum(
            max(0, min(end, right) - max(start, left)) for left, right in covered
        )
        duration = end - start - excluded_ns
        parent = parents[-1][1] if parents else "other"
        elapsed[parent] -= duration
        elapsed[operation] += duration
        parents.append((end, operation))
    if min(elapsed.values()) < 0 or sum(elapsed.values()) != available:
        raise RuntimeError("Measured times do not reconcile to the available finalization time")
    return calls, elapsed


def _split_time(windows, events, excluded, operations):
    calls, elapsed = _split_ns(windows, events, excluded, operations)
    return {"calls": calls, "ms": {op: ns / 1e6 for op, ns in elapsed.items()}}


def split_orm_time(windows, orm_events, driver_events):
    """Assign each non-driver nanosecond to its innermost measured ORM scope."""
    excluded = []
    for start, end, _ in driver_events:
        if excluded and start < excluded[-1][1]:
            raise RuntimeError("Driver calls must be disjoint and ordered")
        excluded.append((start, end))
    return _split_time(windows, orm_events, excluded, ORM_OPERATIONS)


def split_query_time(windows, query_events, orm_events, driver_events):
    """Distinguish query operations only in the remainder of the ORM breakdown."""
    excluded = [(start, end) for start, end, _ in chain(orm_events, driver_events)]
    return _split_time(windows, query_events, excluded, QUERY_OPERATIONS)


def _outside_consumption(windows, result_events):
    """Exclude each slice whose innermost existing result operation is not consumption."""
    for start, end in windows:
        events = [event for event in result_events if start <= event[0] <= event[1] <= end]
        # An inner scope has the shortest extent. Stable sorting preserves return
        # order for coincident scopes, where the inner call records its event first.
        events.sort(key=lambda event: event[1] - event[0])
        boundaries = {start, end}
        for left, right, _ in events:
            boundaries.update((left, right))
        for left, right in pairwise(sorted(boundaries)):
            owner = next((kind for a, b, kind in events if a <= left and right <= b), None)
            if owner != "consumption":
                yield left, right


def split_result_time(windows, result_events, query_events, orm_events, driver_events,
                      *, profile_detail=False, consumption_events=None):
    """Partition the query remainder between eligibility, result processing and other work."""
    excluded = [(start, end) for start, end, _ in chain(query_events, orm_events, driver_events)]
    if not profile_detail and consumption_events is None:
        return _split_time(windows, result_events, excluded, RESULT_OPERATIONS)
    # Only the labels change. Splitting all four kinds of event together preserves
    # nesting between eligibility and consumption, including coincident timestamps.
    calls, ns = _split_ns(windows, result_events, excluded, ("eligibility", *RESULT_DETAIL_OPERATIONS))
    result_calls = {k: calls[k] for k in RESULT_DETAIL_OPERATIONS}
    result_ns = {k: ns[k] for k in RESULT_DETAIL_OPERATIONS}
    detail = {
        "calls": {"eligibility": calls["eligibility"], "results": sum(result_calls.values())},
        "ms": {
            "eligibility": ns["eligibility"] / 1e6, "results": sum(result_ns.values()) / 1e6,
            "other": ns["other"] / 1e6,
        },
        "result_detail_calls": result_calls,
        "result_detail_ms": {k: value / 1e6 for k, value in result_ns.items()},
    }
    if consumption_events is not None:
        excluded.extend(_outside_consumption(windows, result_events))
        calls, parts = _split_ns(windows, consumption_events, excluded, CONSUMPTION_OPERATIONS)
        if sum(parts.values()) != result_ns["consumption"]:
            raise RuntimeError("Row-consumption intervals do not reconcile to their parent")
        detail.update(consumption_calls=calls, consumption_ms={k: value / 1e6 for k, value in parts.items()})
    return detail


def split_overhead_time(windows, overhead_events, result_events, query_events, orm_events, driver_events):
    """Count only the parts of wrapper envelopes outside every measured operation."""
    excluded = [event[:2] for event in chain(result_events, query_events, orm_events, driver_events)]
    return _split_time(windows, overhead_events, excluded, ("bookkeeping",))


def _sql_operation(statement):
    """Recognize SQL sent by the pinned finalizer without examining bound values."""
    sql = " ".join(statement.split())
    if sql.startswith("SELECT "):
        _, _, source = sql.partition(" FROM ")
        if source.startswith("trace_info WHERE trace_info.request_id = ") and sql.endswith(" FOR UPDATE"):
            return "lock_trace"
        if source.startswith("trace_tags WHERE trace_tags.request_id IN ("):
            return "read_trace_tags"
        if source.startswith("trace_tags WHERE trace_tags.request_id = ") and " AND trace_tags.key = " in source:
            return "lookup_tag"
        if source.startswith("spans WHERE spans.trace_id = ") and " AND spans.content != " in source:
            return "check_span_content"
    for prefix, operation in (
        ("UPDATE spans SET content=", "clear_span_content"),
        ("UPDATE trace_tags SET value=", "update_tag"),
        ("INSERT INTO trace_tags (", "insert_tag"),
        ("DELETE FROM trace_tags WHERE ", "delete_tag"),
    ):
        if sql.startswith(prefix):
            return operation
    return "other_sql"


class FinalizeProbe:
    def __init__(self, dialect):
        self.dialect = dialect
        self.events = []
        self.active = False

    def install(self, stack):
        for name, operation in (
            ("do_execute", "sql"), ("do_execute_no_params", "sql"), ("do_executemany", "sql"),
            ("do_commit", "commit"), ("do_rollback", "rollback"),
        ):
            wrapped = self._observe(getattr(self.dialect, name), operation)
            stack.enter_context(patch.object(self.dialect, name, new=wrapped))

    def _observe(self, call, operation):
        def timed(*args, **kwargs):
            if not self.active:
                return call(*args, **kwargs)
            start = perf_counter_ns()
            try:
                return call(*args, **kwargs)
            finally:
                end = perf_counter_ns()
                # SQLAlchemy passes the SQL string after the cursor. Keep no bindings.
                statement = None
                if operation == "sql":
                    statement = args[1] if len(args) > 1 else kwargs["statement"]
                self.events.append((start, end, operation, statement))
        return timed

    def wrap_finalize(self, call):
        def finalize(*args, **kwargs):
            if self.active:
                raise RuntimeError("Unexpected nested archival finalization")
            self.active = True
            try:
                return call(*args, **kwargs)
            finally:
                self.active = False
        return finalize

    def summarize(self, intervals, *, orm_events=None, query_events=None, result_events=None,
                  profile_result_detail=False, overhead_events=None, consumption_events=None):
        """Check call boundaries after each pass and reconcile to its exact outer timers."""
        calls = dict.fromkeys(FINALIZE_OPERATIONS, 0)
        elapsed = dict.fromkeys(FINALIZE_OPERATIONS, 0)
        tag_reads = []
        index = 0
        total = 0
        for phase, start, end in intervals:
            if phase != "db_finalize":
                continue
            previous_end = start
            commits_before = calls["commit"]
            total += end - start
            while index < len(self.events) and self.events[index][0] < end:
                before, after, operation, statement = self.events[index]
                if not previous_end <= before <= after <= end:
                    raise RuntimeError("Driver calls overlap or fall outside database finalization")
                if operation == "sql":
                    operation = _sql_operation(statement)
                if consumption_events is not None and operation == "read_trace_tags":
                    tag_reads.append((before, after))
                calls[operation] += 1
                elapsed[operation] += after - before
                previous_end = after
                index += 1
            if calls["commit"] != commits_before + 1:
                raise RuntimeError("Expected one driver commit per archived trace")
        if index != len(self.events):
            raise RuntimeError("Found driver calls outside measured finalization windows")
        elapsed["other"] = total - sum(elapsed.values())
        summary = {"calls": calls, "ms": {op: ns / 1e6 for op, ns in elapsed.items()}}
        if orm_events is None:
            return summary
        windows = [(start, end) for phase, start, end in intervals if phase == "db_finalize"]
        drivers = [(start, end, op) for start, end, op, _ in self.events]
        detail = split_orm_time(windows, orm_events, drivers)
        summary.update(orm_ms=detail["ms"], orm_calls=detail["calls"])
        if query_events is None:
            return summary
        detail = split_query_time(windows, query_events, orm_events, drivers)
        summary.update(query_ms=detail["ms"], query_calls=detail["calls"])
        if result_events is not None:
            detail = split_result_time(windows, result_events, query_events, orm_events, drivers,
                                       profile_detail=profile_result_detail, consumption_events=consumption_events)
            summary.update(result_ms=detail["ms"], result_calls=detail["calls"])
            if profile_result_detail or consumption_events is not None:
                summary.update(result_detail_ms=detail["result_detail_ms"],
                               result_detail_calls=detail["result_detail_calls"])
            if consumption_events is not None:
                loads = sorted((a, b) for a, b, kind in consumption_events if kind == "eager_tags")
                if any(previous[1] > following[0] for previous, following in pairwise(loads)):
                    raise RuntimeError("Unexpected overlapping eager tag loads")
                if len(loads) != len(tag_reads) or any(
                    not start <= before <= after <= end
                    for (start, end), (before, after) in zip(loads, tag_reads)
                ):
                    raise RuntimeError("Expected one trace-tag SELECT inside each eager tag load")
                summary.update(consumption_ms=detail["consumption_ms"], consumption_calls=detail["consumption_calls"],
                               eager_tag_load_ms=sum(end - start for start, end in loads) / 1e6)
            if overhead_events is not None:
                timed_calls = len(drivers) + len(orm_events) + len(query_events) + len(result_events)
                if len(overhead_events) != timed_calls:
                    raise RuntimeError("Expected one wrapper envelope per measured finalization call")
                overhead = split_overhead_time(windows, overhead_events, result_events,
                                               query_events, orm_events, drivers)
                summary.update(overhead_ms=overhead["ms"], overhead_calls=overhead["calls"])
        return summary
