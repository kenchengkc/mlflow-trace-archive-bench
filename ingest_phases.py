"""Time exclusive parts of a single-span write on the pinned MLflow/PostgreSQL path."""
from contextlib import ExitStack, contextmanager
from time import perf_counter_ns
from unittest.mock import patch

import chunk_ingest


PHASES = ("build_span", "translate_span", "encode_content", "span_write", "other_write", "other_sql", "commit")


class WriterPhases:
    """One synchronous writer process. Warmup calls outside trace() are ignored."""

    def __init__(self):
        self.samples = []
        self._sample = None
        self._running_phase = None

    @contextmanager
    def trace(self):
        if self._sample is not None:
            raise RuntimeError("A trace is already being measured")
        self._sample = {"duration_ns": dict.fromkeys(PHASES, 0), "calls": dict.fromkeys(PHASES, 0)}
        try:
            yield
            self.samples.append(self._sample)
        finally:
            self._sample = None

    def measure(self, phase, call, *args, **kwargs):
        if self._sample is None:
            return call(*args, **kwargs)
        if self._running_phase is not None:
            raise RuntimeError(f"Overlapping phases: {self._running_phase} and {phase}")
        self._running_phase = phase
        start = perf_counter_ns()
        try:
            return call(*args, **kwargs)
        finally:
            elapsed = perf_counter_ns() - start
            self._running_phase = None
            self._sample["duration_ns"][phase] += elapsed
            self._sample["calls"][phase] += 1


def _timed(recorder, phase, call):
    def measured(*args, **kwargs):
        return recorder.measure(phase, call, *args, **kwargs)
    return measured


def _timed_execute(recorder, execute):
    def measured(cursor, statement, *args, **kwargs):
        # Match the SQL actually sent on this pinned store path, including
        # INSERT ... ON CONFLICT for spans. Bound payloads live in args.
        statement_start = statement.lstrip()
        if statement_start.startswith("INSERT INTO spans "):
            phase = "span_write"
        elif statement_start.startswith(("INSERT ", "UPDATE ", "DELETE ")):
            phase = "other_write"
        else:
            phase = "other_sql"
        return recorder.measure(phase, execute, cursor, statement, *args, **kwargs)
    return measured


@contextmanager
def capture_writer_phases(engine):
    """Wrap the writer's real functions for one trial and restore them on exit."""
    from mlflow.store.tracking import sqlalchemy_store

    recorder = WriterPhases()

    # Only this module's content_json = json.dumps(..., cls=TraceJSONEncoder)
    # uses this binding. Keep JSON encoding elsewhere in MLflow untouched.
    class TimedContentEncoder(sqlalchemy_store.TraceJSONEncoder):
        def encode(self, obj):
            return recorder.measure("encode_content", super().encode, obj)

    with ExitStack() as stack:
        for owner, name, phase in (
            (chunk_ingest, "build_span", "build_span"),
            (sqlalchemy_store, "translate_span_when_storing", "translate_span"),
            (engine.dialect, "do_commit", "commit"),
        ):
            wrapper = _timed(recorder, phase, getattr(owner, name))
            stack.enter_context(patch.object(owner, name, wrapper))
        stack.enter_context(patch.object(sqlalchemy_store, "TraceJSONEncoder", TimedContentEncoder))
        # These dialect calls execute the DBAPI cursor operations. do_commit
        # forwards to the DBAPI connection after ORM flush has finished.
        for name in ("do_execute", "do_execute_no_params", "do_executemany"):
            wrapper = _timed_execute(recorder, getattr(engine.dialect, name))
            stack.enter_context(patch.object(engine.dialect, name, wrapper))
        yield recorder
