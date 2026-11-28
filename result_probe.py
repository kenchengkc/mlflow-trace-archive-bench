"""Time MLflow eligibility checks and SQLAlchemy result handling during finalization."""

from time import perf_counter_ns
from unittest.mock import patch

from orm_probe import OrmProbe


class ResultProbe(OrmProbe):
    def install(self, stack):
        from mlflow.store.tracking.dbmodels.models import SqlTraceInfo
        from mlflow.store.tracking.sqlalchemy_store import SqlAlchemyStore
        from sqlalchemy.engine.result import Result, ResultInternal, ScalarResult
        from sqlalchemy.orm import Query
        from sqlalchemy.orm.bulk_persistence import BulkUDCompileState
        from sqlalchemy.orm.context import ORMCompileState

        # The pinned PostgreSQL finalizer checks the returned header and its
        # generation inline, immediately before calling the metadata guard.
        # Start at the return from the locked query so its execution stays out.
        header_return = None
        finalize_trace = SqlAlchemyStore._finalize_archived_trace
        one_or_none = Query.one_or_none

        def finalize(store, **kwargs):
            nonlocal header_return
            try:
                return finalize_trace(store, **kwargs)
            finally:
                # Drop the marker even if the inline guards return early.
                header_return = None

        def get_header(query):
            nonlocal header_return
            if not self.finalize_probe.active or query._for_update_arg is None:
                return one_or_none(query)
            header_return = None
            row = one_or_none(query)
            if isinstance(row, SqlTraceInfo):
                header_return = (row, perf_counter_ns())
            return row

        check_metadata = SqlAlchemyStore._is_trace_metadata_actionable_for_archival

        def metadata(store, sql_trace_info):
            nonlocal header_return
            if not self.finalize_probe.active:
                return check_metadata(store, sql_trace_info)
            if header_return is not None and header_return[0] is sql_trace_info:
                start = header_return[1]
            else:
                start = perf_counter_ns()
            header_return = None
            try:
                return check_metadata(store, sql_trace_info)
            finally:
                self.events.append((start, perf_counter_ns(), "eligibility"))

        stack.enter_context(patch.object(SqlAlchemyStore, "_finalize_archived_trace", new=finalize))
        stack.enter_context(patch.object(Query, "one_or_none", new=get_header))
        stack.enter_context(patch.object(SqlAlchemyStore, "_is_trace_metadata_actionable_for_archival", new=metadata))

        hooks = (
            (SqlAlchemyStore, "eligibility", ("_trace_has_non_empty_span_content",)),
            (Result, "results", ("scalars", "unique")),
            (ScalarResult, "results", ("unique",)),
            (ResultInternal, "results", ("_only_one_row",)),
        )
        for owner, category, names in hooks:
            for name in names:
                wrapped = self._observe(getattr(owner, name), category)
                stack.enter_context(patch.object(owner, name, new=wrapped))

        # SQLAlchemy calls these classmethods after Connection.execute returns.
        # They set up SELECT row processors or adapt the bulk UPDATE/DELETE result.
        for owner in (ORMCompileState, BulkUDCompileState):
            setup = owner.orm_setup_cursor_result.__func__
            wrapped = classmethod(self._observe(setup, "results"))
            stack.enter_context(patch.object(owner, "orm_setup_cursor_result", new=wrapped))
