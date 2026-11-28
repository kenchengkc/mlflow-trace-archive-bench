"""Time SQLAlchemy's select-in loader while finalization consumes a trace's tags."""

import ast
from contextlib import contextmanager
from inspect import getsourcefile, getsourcelines
from textwrap import dedent
from time import perf_counter_ns
from unittest.mock import patch


def _instrument(original, probe, transform):
    lines, first_line = getsourcelines(original)
    tree = ast.parse(dedent("".join(lines)))
    transform(tree.body[0].body, probe)
    ast.fix_missing_locations(tree)
    ast.increment_lineno(tree, first_line - 1)
    namespace = dict(original.__globals__, _tag_probe=probe, _tag_clock=perf_counter_ns,
                     _tag_record=probe.prep_events.append)
    exec(compile(tree, getsourcefile(original), "exec"), namespace)
    return namespace[original.__name__]


def _timed_preparation(kind, body):
    # Inline clocks keep the cost of recording the event outside its interval.
    def clock():
        return ast.Call(ast.Name("_tag_clock", ast.Load()), [], [])

    start = ast.Assign([ast.Name("_tag_start", ast.Store())], clock())
    event = ast.Tuple([ast.Name("_tag_start", ast.Load()), clock(), ast.Constant(kind)], ast.Load())
    stop = ast.Expr(ast.Call(ast.Name("_tag_record", ast.Load()), [event], []))
    return [ast.copy_location(start, body[0]), *body, ast.copy_location(stop, body[-1])]


def _instrument_path(body, probe):
    # Split the installed method at the transitions between state collection,
    # statement construction, loader options and the final loader dispatch.
    landmarks = ("query_info = self._query_info", "pk_cols = query_info.pk_cols",
                 "new_options = context._loader_strategy_options()")
    statements = [ast.unparse(stmt) for stmt in body]
    if any(statements.count(mark) != 1 for mark in landmarks):
        raise RuntimeError("Unrecognized SQLAlchemy select-in preparation")
    info, select, options = (statements.index(mark) for mark in landmarks)
    order = len(body) - 2
    if not (info + 2 < select < options < order
            and isinstance(body[info + 1], ast.If) and ast.unparse(body[info + 1].test) == "query_info.load_only_child"
            and isinstance(body[select - 1], ast.If) and ast.unparse(body[select - 1].test) == "not query_info.load_only_child"
            and any(s.startswith("q = Select._create_raw_select(") for s in statements[select:options])
            and statements[options - 1].startswith("q = q.filter(")
            and "q = q.options(*new_options)" in statements[options:order]
            and any(s.startswith("q = q._update_compile_options(") for s in statements[options:order])
            and isinstance(body[order], ast.If) and ast.unparse(body[order].test) == "self.parent_property.order_by"
            and isinstance(body[-1], ast.If) and ast.unparse(body[-1].test) == "query_info.load_only_child"
            and statements[-1].count("self._load_via_parent(") == 1):
        raise RuntimeError("Unrecognized SQLAlchemy select-in preparation boundaries")
    body[:] = [*body[:info + 1], *_timed_preparation("parent_keys", body[info + 1:select]),
               *_timed_preparation("select_construction", body[select:options]),
               *_timed_preparation("loader_options", body[options:order]), *body[order:]]


def _instrument_parent(body, probe):
    # Preserve the pinned loader's key batching, group iteration, assignment and
    # skip paths. The SELECT sits inside the groupby expression in SQLAlchemy.
    batches = [n for n in body if isinstance(n, ast.While) and ast.unparse(n.test) == "our_states"]
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
    if probe.profile_preparation:
        assignments = [ast.unparse(stmt.targets[0]) for stmt in batch.body[:-2] if isinstance(stmt, ast.Assign)]
        params = [ast.unparse(kw.value) for kw in execute.keywords if kw.arg == "params"]
        if (len(batch.body) != 6 or assignments != ["chunk", "our_states", "primary_keys", "data"]
                or params != ["{'primary_keys': primary_keys}"]):
            raise RuntimeError("Unrecognized SQLAlchemy parent-key batching")
        batch.body[:3] = _timed_preparation("parent_keys", batch.body[:3])
    execute.args.insert(0, execute.func)
    execute.func = ast.Attribute(ast.Name("_tag_probe", ast.Load()), "_execute", ast.Load())
    groups.iter.args[0] = call("_group_rows", unique)
    batch.body.insert(0, ast.Expr(call("_begin_batch")))
    batch.body.insert(-1, ast.Expr(call("_grouped", ast.Name("data", ast.Load()))))

    # A continue for an already populated header still skips the counter.
    timed = ast.With([ast.withitem(call("_attachment"))], attach.body)
    attach.body = [ast.copy_location(timed, attach.body[0]),
                   ast.Expr(call("_attached", ast.Name("collection", ast.Load())))]
    if probe.profile_preparation:
        batch.body.append(ast.Expr(call("_batched", ast.Name("primary_keys", ast.Load()))))


class TagLoadProbe:
    def __init__(self, finalize_probe, *, profile_preparation=False):
        self.finalize_probe = finalize_probe
        self.profile_preparation = profile_preparation
        self.events = []
        self.prep_events = []
        self._loader = None
        self._start = None
        self.reset()

    def reset(self):
        self.events.clear()
        self.prep_events.clear()
        self.prep_counts = dict.fromkeys(("key_batches", "keys_batched"), 0)
        self.counts = dict.fromkeys((
            "tag_rows_grouped", "parent_key_groups", "headers_attached", "empty_collections_attached",
        ), 0)

    def wrap_load(self, load):
        """Called inside ConsumptionProbe's matched tag-load timer."""
        from mlflow.store.tracking.dbmodels.models import SqlTraceInfo

        tags = SqlTraceInfo.__mapper__.relationships["tags"]
        matched_load = _instrument(load, self, _instrument_path) if self.profile_preparation else load

        def matched(loader, *args, **kwargs):
            if not self.finalize_probe.active or loader.parent_property is not tags:
                return load(loader, *args, **kwargs)
            previous = self._loader, self._start
            self._loader, self._start = loader, perf_counter_ns()
            try:
                return matched_load(loader, *args, **kwargs)
            finally:
                self._loader, self._start = previous
        return matched

    def install(self, stack):
        from sqlalchemy.orm.strategies import SelectInLoader

        original = SelectInLoader._load_via_parent
        instrumented = _instrument(original, self, _instrument_parent)

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

    def _batched(self, primary_keys):
        self.prep_counts["key_batches"] += 1
        self.prep_counts["keys_batched"] += len(primary_keys)
