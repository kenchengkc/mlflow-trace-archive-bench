"""Attribute finalization wall time to psycopg2 calls on the benchmark's synchronous store."""

from time import perf_counter_ns
from unittest.mock import patch


FINALIZE_OPERATIONS = (
    "lock_trace", "read_trace_tags", "check_span_content", "clear_span_content",
    "update_tag", "lookup_tag", "insert_tag", "delete_tag", "other_sql", "commit", "rollback",
)
ORM_OPERATIONS = ("fetch", "objects", "session")


def split_orm_time(windows, orm_events, driver_events):
    """Assign each non-driver nanosecond to its innermost measured ORM scope."""
    calls = dict.fromkeys(ORM_OPERATIONS, 0)
    elapsed = dict.fromkeys((*ORM_OPERATIONS, "other"), 0)
    previous_end = None
    for start, end in windows:
        if end < start or (previous_end is not None and start < previous_end):
            raise RuntimeError("Finalization windows overlap or have negative duration")
        previous_end = end
    for start, end, _ in (*orm_events, *driver_events):
        if not any(left <= start <= end <= right for left, right in windows):
            raise RuntimeError("Recorded call falls outside a finalization window")
    previous_end = None
    for start, end, _ in driver_events:
        if previous_end is not None and start < previous_end:
            raise RuntimeError("Driver calls must be disjoint and ordered")
        previous_end = end

    driver_ns = sum(end - start for start, end, _ in driver_events)
    outside_driver = sum(end - start for start, end in windows) - driver_ns
    elapsed["other"] = outside_driver
    parents = []
    # The probe records on return. For equal start and end timestamps, visit
    # the last recorded (outermost) scope first.
    for start, end, operation in sorted(reversed(orm_events), key=lambda event: (event[0], -event[1])):
        calls[operation] += 1
        if start == end:
            continue
        while parents and parents[-1][0] <= start:
            parents.pop()
        if parents and end > parents[-1][0]:
            raise RuntimeError("ORM intervals overlap without nesting")
        # Transfer inclusive, driver-free time from the parent to the child.
        # Each deeper child will receive its own share in turn.
        nested_driver_ns = sum(
            max(0, min(end, right) - max(start, left)) for left, right, _ in driver_events
        )
        duration = end - start - nested_driver_ns
        parent = parents[-1][1] if parents else "other"
        elapsed[parent] -= duration
        elapsed[operation] += duration
        parents.append((end, operation))
    if min(elapsed.values()) < 0 or sum(elapsed.values()) != outside_driver:
        raise RuntimeError("ORM times do not reconcile to finalization time outside driver calls")
    return {"calls": calls, "ms": {op: ns / 1e6 for op, ns in elapsed.items()}}


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

    def summarize(self, intervals, *, orm_events=None):
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
        return summary
