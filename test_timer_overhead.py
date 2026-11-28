import unittest
from types import SimpleNamespace
from unittest.mock import patch

from finalize_probe import _split_time
from orm_probe import OrmProbe
from timer_overhead import TimerOverhead


class TimerOverheadTest(unittest.TestCase):
    def test_only_exposed_edges_of_nested_timers_count(self):
        gate = SimpleNamespace(active=False)
        probe = OrmProbe(gate)
        overhead = TimerOverhead(gate)
        overhead.instrument(probe)
        inner = probe._observe(lambda value: value, "fetch")
        outer = probe._observe(inner, "objects")
        with (
            patch("timer_overhead.perf_counter_ns", side_effect=[2, 7, 16, 21]),
            patch("orm_probe.perf_counter_ns", side_effect=[4, 8, 14, 18]),
        ):
            self.assertEqual(outer(41), 41)
            self.assertEqual((overhead.events, probe.events), ([], []))
            gate.active = True
            self.assertEqual(outer(42), 42)

        self.assertEqual(overhead.events, [(7, 16, "bookkeeping"), (2, 21, "bookkeeping")])
        self.assertEqual(probe.events, [(8, 14, "fetch"), (4, 18, "objects")])
        masked = [(start, end) for start, end, _ in probe.events]
        detail = _split_time([(0, 25)], overhead.events, masked, ("bookkeeping",))
        # Both inner edges stay inside the outer operation. Only the outer
        # wrapper edges [2,4) and [18,21) count towards bookkeeping.
        self.assertEqual(detail["calls"]["bookkeeping"], 2)
        self.assertEqual(round(detail["ms"]["bookkeeping"] * 1e6), 5)
        self.assertEqual(round(detail["ms"]["other"] * 1e6), 6)


if __name__ == "__main__":
    unittest.main()
