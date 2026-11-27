"""Measure SQLAlchemy statement construction, compiled-cache access, and execution dispatch."""

from time import perf_counter_ns
from unittest.mock import patch

from orm_probe import OrmProbe


class QueryProbe(OrmProbe):
    def __init__(self, finalize_probe):
        super().__init__(finalize_probe)
        from sqlalchemy.engine.interfaces import CacheStats

        self.cache_counts = dict.fromkeys((outcome.name.lower() for outcome in CacheStats), 0)

    def reset(self):
        self.events.clear()
        for outcome in self.cache_counts:
            self.cache_counts[outcome] = 0

    def install(self, stack):
        from sqlalchemy.engine import Connection
        from sqlalchemy.orm import Query, Session, strategy_options
        from sqlalchemy.orm.attributes import QueryableAttribute
        from sqlalchemy.orm.bulk_persistence import BulkUDCompileState
        from sqlalchemy.orm.context import ORMCompileState
        from sqlalchemy.orm.query import BulkUD
        from sqlalchemy.sql import ClauseElement, Delete, Select, Update

        # Measure the builder methods directly. Query.update(), delete(), first()
        # and one_or_none() would also include execution or result consumption.
        hooks = (
            (Query, "construction", (
                "__init__", "with_for_update", "filter", "options", "limit", "_statement_20",
            )),
            (QueryableAttribute, "construction", ("operate",)),
            (strategy_options, "construction", ("_generate_from_keys",)),
            (BulkUD, "construction", ("__init__",)),
            (Update, "construction", ("__init__", "values")),
            (Delete, "construction", ("__init__",)),
            (Select, "construction", ("__init__", "where", "options")),
            (ClauseElement, "compilation", ("_compiler",)),
            (Session, "dispatch", ("get_bind",)),
            (Connection, "dispatch", ("execute",)),
        )
        for owner, category, names in hooks:
            for name in names:
                wrapped = self._observe(getattr(owner, name), category)
                stack.enter_context(patch.object(owner, name, new=wrapped))

        # These classmethods process ORM execution options before the call to
        # Connection.execute(). Keep the subclass supplied by SQLAlchemy.
        for owner in (ORMCompileState, BulkUDCompileState):
            prepare = owner.orm_pre_session_exec.__func__
            wrapped = classmethod(self._observe(prepare, "dispatch"))
            stack.enter_context(patch.object(owner, "orm_pre_session_exec", new=wrapped))

        compile_with_cache = ClauseElement._compile_w_cache

        def lookup(*args, **kwargs):
            if not self.finalize_probe.active:
                return compile_with_cache(*args, **kwargs)
            start = perf_counter_ns()
            try:
                result = compile_with_cache(*args, **kwargs)
            finally:
                self.events.append((start, perf_counter_ns(), "compile_cache"))
            # The returned enum distinguishes a hit, a miss, and bypasses.
            # A miss nests the separate _compiler scope inside this interval.
            self.cache_counts[result[2].name.lower()] += 1
            return result

        stack.enter_context(patch.object(ClauseElement, "_compile_w_cache", new=lookup))
