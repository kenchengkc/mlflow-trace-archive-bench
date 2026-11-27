"""Attribute finalization wall time to nested SQLAlchemy and psycopg2 operations."""

from itertools import chain
from time import perf_counter_ns
from unittest.mock import patch


FINALIZE_OPERATIONS = (
    "lock_trace", "read_trace_tags", "check_span_content", "clear_span_content",
    "update_tag", "lookup_tag", "insert_tag", "delete_tag", "other_sql", "commit", "rollback",
)
ORM_OPERATIONS = ("fetch", "objects", "session")
QUERY_OPERATIONS = ("construction", "compile_cache", "compilation", "dispatch")


def _split_time(windows, events, excluded, operations):
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

    # Earlier timers keep their time even when a new query hook runs inside one.
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

    def summarize(self, intervals, *, orm_events=None, query_events=None):
        """Check call boundaries after each pass and reconcile to its exact outer timers."""
        calls = dict.fromkeys(FINALIZE_OPERATIONS, 0)
        elapsed = dict.fromkeys(FINALIZE_OPERATIONS, 0)
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
        if orm_events is not None:
            windows = [(start, end) for phase, start, end in intervals if phase == "db_finalize"]
            drivers = [(start, end, op) for start, end, op, _ in self.events]
            detail = split_orm_time(windows, orm_events, drivers)
            summary.update(orm_ms=detail["ms"], orm_calls=detail["calls"])
            if query_events is not None:
                detail = split_query_time(windows, query_events, orm_events, drivers)
                summary.update(query_ms=detail["ms"], query_calls=detail["calls"])
        return summary
