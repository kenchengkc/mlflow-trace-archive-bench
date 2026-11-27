"""Time selected SQLAlchemy operations inside synchronous MLflow trace finalization."""

from time import perf_counter_ns
from unittest.mock import patch


class OrmProbe:
    def __init__(self, finalize_probe):
        self.finalize_probe = finalize_probe
        self.events = []

    def _observe(self, call, category):
        def measured(*args, **kwargs):
            if not self.finalize_probe.active:
                return call(*args, **kwargs)
            start = perf_counter_ns()
            try:
                return call(*args, **kwargs)
            finally:
                self.events.append((start, perf_counter_ns(), category))
        return measured

    def install(self, stack):
        from mlflow.store.tracking.dbmodels.models import SqlTraceTag
        from sqlalchemy.engine.cursor import CursorFetchStrategy, CursorResult
        from sqlalchemy.orm import Session, loading
        from sqlalchemy.orm.instrumentation import ClassManager

        # In this workload _raw_all_rows wraps fetched tuples as SQLAlchemy Rows.
        # The inner fetch strategy includes cursor cleanup after the last row.
        hooks = (
            (CursorFetchStrategy, "fetch", ("fetchone", "fetchmany", "fetchall")),
            (CursorResult, "objects", ("_raw_all_rows",)),
            (ClassManager, "objects", ("new_instance",)),
            (SqlTraceTag, "objects", ("__init__",)),
            (Session, "session", (
                "__init__", "_autobegin_t", "_connection_for_bind", "merge",
                "flush", "commit", "close",
            )),
        )
        for owner, category, names in hooks:
            for name in names:
                wrapped = self._observe(getattr(owner, name), category)
                stack.enter_context(patch.object(owner, name, new=wrapped))

        # _instance_processor builds a callable for this query execution. Time the
        # callable that actually looks up and populates each mapped object.
        instance_processor = loading._instance_processor

        def build_processor(*args, **kwargs):
            process_row = instance_processor(*args, **kwargs)
            if self.finalize_probe.active:
                return self._observe(process_row, "objects")
            return process_row

        stack.enter_context(patch.object(loading, "_instance_processor", new=build_processor))
