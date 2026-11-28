"""Bracket the benchmark's existing timers to observe time at their edges."""

from time import perf_counter_ns
from unittest.mock import patch


class TimerOverhead:
    def __init__(self, finalize_probe):
        self.finalize_probe = finalize_probe
        self.events = []

    def _enclose(self, timer):
        def envelope(*args, **kwargs):
            if not self.finalize_probe.active:
                return timer(*args, **kwargs)
            start = perf_counter_ns()
            try:
                return timer(*args, **kwargs)
            finally:
                end = perf_counter_ns()
                self.events.append((start, end, "bookkeeping"))
        return envelope

    def instrument(self, probe):
        """Bracket each timer that this probe creates, including per-row processors."""
        observe = probe._observe

        def make_timer(call, operation):
            return self._enclose(observe(call, operation))

        probe._observe = make_timer

    def install_custom(self, stack):
        """Bracket the two timers constructed outside the probes' _observe factories."""
        from mlflow.store.tracking.sqlalchemy_store import SqlAlchemyStore
        from sqlalchemy.sql import ClauseElement

        # The cache timer records its outcome after the original call. The
        # metadata timer can start before method entry at the locked header's
        # return. In both cases its existing event covers the application work.
        for owner, method in (
            (ClauseElement, "_compile_w_cache"),
            (SqlAlchemyStore, "_is_trace_metadata_actionable_for_archival"),
        ):
            timer = getattr(owner, method)
            stack.enter_context(patch.object(owner, method, new=self._enclose(timer)))
