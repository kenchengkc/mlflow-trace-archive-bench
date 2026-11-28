"""Time SQLAlchemy's real single-row fetches, distinct-row check and eager tag loader."""

import ast
from contextlib import contextmanager
from inspect import getsourcefile, getsourcelines
from textwrap import dedent
from time import perf_counter_ns
from unittest.mock import patch


def _instrument_consumer(original, advance, uniqueness, comparison):
    # The unique branch is inline. Insert observation into the installed method so
    # SQLAlchemy still controls row iteration, early returns, and exceptions.
    lines, first_line = getsourcelines(original)
    tree = ast.parse(dedent("".join(lines)))
    pulls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "onerow"]
    conditions = [n for n in ast.walk(tree) if isinstance(n, ast.If)]
    branches = [n for n in conditions if ast.unparse(n.test) == "self._unique_filter_state"]
    compares = [n for n in conditions if ast.unparse(n.test) in (
        "existing_row_hash == strategy(next_row)", "row == next_row",
    )]
    if (len(pulls), len(branches), len(compares)) != (3, 1, 2):
        raise RuntimeError("Unrecognized SQLAlchemy _only_one_row implementation")

    for pull in pulls:
        pull.args.insert(0, pull.func)
        pull.func = ast.Name("_bench_advance", ast.Load())
    for check in compares:
        check.test = ast.Call(ast.Name("_bench_comparison", ast.Load()), [check.test], [])
    branch, = branches
    clock = ast.Call(ast.Name("_bench_uniqueness", ast.Load()), [], [])
    timed = ast.With([ast.withitem(clock)], branch.body)
    branch.body = [ast.copy_location(timed, branch.body[0])]

    ast.fix_missing_locations(tree)
    ast.increment_lineno(tree, first_line - 1)
    namespace = dict(original.__globals__, _bench_advance=advance,
                     _bench_uniqueness=uniqueness, _bench_comparison=comparison)
    exec(compile(tree, getsourcefile(original), "exec"), namespace)
    return namespace[original.__name__]


class ConsumptionProbe:
    def __init__(self, finalize_probe, *, tag_load_probe=None):
        self.finalize_probe = finalize_probe
        self.tag_load_probe = tag_load_probe
        self.events = []
        self.counts = {}
        self.reset()

    def reset(self):
        self.events.clear()
        self.counts = dict.fromkeys(("rows_returned", "exhausted", "extra_rows_compared"), 0)

    def install(self, stack):
        from mlflow.store.tracking.dbmodels.models import SqlTraceInfo
        from sqlalchemy.engine.result import ResultInternal
        from sqlalchemy.orm.strategies import SelectInLoader

        def advance(onerow, *, hard_close):
            start = perf_counter_ns()
            try:
                row = onerow(hard_close=hard_close)
            finally:
                self.events.append((start, perf_counter_ns(), "iteration"))
            self.counts["exhausted" if row is None else "rows_returned"] += 1
            return row

        @contextmanager
        def uniqueness():
            start = perf_counter_ns()
            try:
                yield
            finally:
                self.events.append((start, perf_counter_ns(), "uniqueness"))

        def comparison(outcome):
            self.counts["extra_rows_compared"] += 1
            return outcome

        original = ResultInternal._only_one_row
        instrumented = _instrument_consumer(original, advance, uniqueness, comparison)
        consuming = False

        def consume(*args, **kwargs):
            nonlocal consuming
            if not self.finalize_probe.active:
                return original(*args, **kwargs)
            previous, consuming = consuming, True
            try:
                return instrumented(*args, **kwargs)
            finally:
                consuming = previous

        tag_relationship = SqlTraceInfo.__mapper__.relationships["tags"]
        load = SelectInLoader._load_for_path
        matched_load = self.tag_load_probe.wrap_load(load) if self.tag_load_probe is not None else load

        def load_tags(loader, *args, **kwargs):
            if not consuming or loader.parent_property is not tag_relationship:
                return load(loader, *args, **kwargs)
            start = perf_counter_ns()
            try:
                return matched_load(loader, *args, **kwargs)
            finally:
                self.events.append((start, perf_counter_ns(), "eager_tags"))

        stack.enter_context(patch.object(ResultInternal, "_only_one_row", new=consume))
        stack.enter_context(patch.object(SelectInLoader, "_load_for_path", new=load_tags))
