"""Attribute finalization wall time to psycopg2 calls on the benchmark's synchronous store."""

from time import perf_counter_ns
from unittest.mock import patch


FINALIZE_OPERATIONS = (
    "lock_trace", "read_trace_tags", "check_span_content", "clear_span_content",
    "update_tag", "lookup_tag", "insert_tag", "delete_tag", "other_sql", "commit", "rollback",
)


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

    def summarize(self, intervals):
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
        return {"calls": calls, "ms": {op: ns / 1e6 for op, ns in elapsed.items()}}
