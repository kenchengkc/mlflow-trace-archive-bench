"""Time SQLAlchemy's select-in loader while finalization consumes a trace's tags."""

import ast
from contextlib import contextmanager
from inspect import getsourcefile, getsourcelines
from textwrap import dedent
from time import perf_counter_ns
from unittest.mock import patch


def _instrument_parent(original, probe):
    # Preserve the pinned loader's key batching, group iteration, assignment and
    # skip paths. The SELECT sits inside the groupby expression in SQLAlchemy.
    lines, first_line = getsourcelines(original)
    tree = ast.parse(dedent("".join(lines)))
    batches = [n for n in ast.walk(tree) if isinstance(n, ast.While) and ast.unparse(n.test) == "our_states"]
    if len(batches) != 1:
        raise RuntimeError("Unrecognized SQLAlchemy select-in batch loop")
    batch, = batches
    groups, attach = batch.body[-2:]
    if not (isinstance(groups, ast.For) and ast.unparse(groups.target) == "(k, v)"
            and isinstance(groups.iter, ast.Call) and ast.unparse(groups.iter.func) == "itertools.groupby"
            and isinstance(attach, ast.For) and ast.unparse(attach.iter) == "chunk"
            and ast.unparse(attach.target) == "(key, state, state_dict, overwrite)"):
        raise RuntimeError("Unrecognized SQLAlchemy grouping or attachment loop")
    unique = groups.iter.args[0]
    if not (isinstance(unique, ast.Call) and isinstance(unique.func, ast.Attribute)
            and unique.func.attr == "unique" and isinstance(unique.func.value, ast.Call)
            and ast.unparse(unique.func.value.func) == "context.session.execute"):
        raise RuntimeError("Unrecognized SQLAlchemy select-in query")

    def call(method, *args):
        return ast.Call(ast.Attribute(ast.Name("_tag_probe", ast.Load()), method, ast.Load()), list(args), [])

    # Stop preparation immediately before dispatch. Start grouping only after
    # execute(...).unique() has returned; keep the original result iterator.
    execute = unique.func.value
    execute.args.insert(0, execute.func)
    execute.func = ast.Attribute(ast.Name("_tag_probe", ast.Load()), "_execute", ast.Load())
    groups.iter.args[0] = call("_group_rows", unique)
    batch.body.insert(0, ast.Expr(call("_begin_batch")))
    batch.body.insert(-1, ast.Expr(call("_grouped", ast.Name("data", ast.Load()))))

    # A continue for an already populated header still skips the counter.
    timed = ast.With([ast.withitem(call("_attachment"))], attach.body)
    attach.body = [ast.copy_location(timed, attach.body[0]),
                   ast.Expr(call("_attached", ast.Name("collection", ast.Load())))]

    ast.fix_missing_locations(tree)
    ast.increment_lineno(tree, first_line - 1)
    namespace = dict(original.__globals__, _tag_probe=probe)
    exec(compile(tree, getsourcefile(original), "exec"), namespace)
    return namespace[original.__name__]


class TagLoadProbe:
    def __init__(self, finalize_probe):
        self.finalize_probe = finalize_probe
        self.events = []
        self._loader = None
        self._start = None
        self.reset()

    def reset(self):
        self.events.clear()
        self.counts = dict.fromkeys((
            "tag_rows_grouped", "parent_key_groups", "headers_attached", "empty_collections_attached",
        ), 0)

    def wrap_load(self, load):
        """Called inside ConsumptionProbe's matched tag-load timer."""
        from mlflow.store.tracking.dbmodels.models import SqlTraceInfo

        tags = SqlTraceInfo.__mapper__.relationships["tags"]

        def matched(loader, *args, **kwargs):
            if not self.finalize_probe.active or loader.parent_property is not tags:
                return load(loader, *args, **kwargs)
            previous = self._loader, self._start
            self._loader, self._start = loader, perf_counter_ns()
            try:
                return load(loader, *args, **kwargs)
            finally:
                self._loader, self._start = previous
        return matched

    def install(self, stack):
        from sqlalchemy.orm.strategies import SelectInLoader

        original = SelectInLoader._load_via_parent
        instrumented = _instrument_parent(original, self)

        def parent(loader, *args, **kwargs):
            if self._loader is loader:
                return instrumented(loader, *args, **kwargs)
            return original(loader, *args, **kwargs)

        stack.enter_context(patch.object(SelectInLoader, "_load_via_parent", new=parent))

    def _begin_batch(self):
        # The first batch includes statement construction in _load_for_path.
        # Subsequent batches prepare their own parent keys for that statement.
        if self._start is None:
            self._start = perf_counter_ns()

    def _execute(self, execute, *args, **kwargs):
        self.events.append((self._start, perf_counter_ns(), "preparation"))
        self._start = None
        return execute(*args, **kwargs)

    def _group_rows(self, result):
        self._start = perf_counter_ns()
        return result

    def _grouped(self, data):
        self.events.append((self._start, perf_counter_ns(), "grouping"))
        self._start = None
        self.counts["parent_key_groups"] += len(data)
        self.counts["tag_rows_grouped"] += sum(map(len, data.values()))

    @contextmanager
    def _attachment(self):
        start = perf_counter_ns()
        try:
            yield
        finally:
            self.events.append((start, perf_counter_ns(), "attachment"))

    def _attached(self, collection):
        self.counts["headers_attached"] += 1
        self.counts["empty_collections_attached"] += int(not collection)
