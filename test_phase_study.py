from collections import Counter
from itertools import groupby, permutations
import unittest

from ingest_phases import PHASES
from phase_study import CONDITIONS, EXPECTED_CALLS, LARGE_BYTES, phase_totals, summarize, trial_schedule


class PhaseStudyTest(unittest.TestCase):
    def test_all_treatments_balance_position_and_within_round_predecessor(self):
        positions = [[] for _ in CONDITIONS]
        pairs = Counter()
        for _, group in groupby(trial_schedule(8), key=lambda row: row[0]):
            row = [tuple(item[1:]) for item in group]
            self.assertCountEqual(row, CONDITIONS)
            for pos, treatment in enumerate(row):
                positions[pos].append(treatment)
            pairs.update(zip(row, row[1:]))
        for treatments in positions:
            self.assertCountEqual(treatments, CONDITIONS)
        self.assertEqual(pairs, Counter(permutations(CONDITIONS, 2)))

    @staticmethod
    def writer(scale, profiled):
        # Two different trace durations per trial and a changing residual.
        writer = {"commit_offsets_ns": [], "elapsed_ns": 0}
        if profiled:
            writer["phase_samples"] = []
        for weight in (1, 3):
            unit = 1000 * scale * weight
            phases = {phase: number * unit for number, phase in enumerate(PHASES, start=1)}
            elapsed = sum(phases.values()) + 9 * unit
            if profiled:
                writer["phase_samples"].append({"duration_ns": phases, "calls": EXPECTED_CALLS.copy()})
            else:
                elapsed -= unit
            writer["elapsed_ns"] += elapsed
            writer["commit_offsets_ns"].append(writer["elapsed_ns"])
        return writer

    def test_costs_are_additive_means_over_all_rounds_traces_and_controls(self):
        report = {"traces_per_writer_trial": 2, "round_count": 3, "trials": []}
        for r, mode, padding, profiled in trial_schedule(3):
            # The mean of the three factors (2) is different from their median (1).
            scale = (1, 1, 4)[r - 1] * (1 if mode == "mixed_1000" else 2)
            if padding == LARGE_BYTES:
                scale *= 3
            report["trials"].append({"round": r, "mode": mode, "padding_bytes": padding,
                                     "profiled": profiled, "writer": self.writer(scale, profiled)})
        summary = summarize(report)
        for size, factor in (("1000", 1), ("1001", 2)):
            for label, mult in (("small", 1), ("64_kib", 3), ("delta", 2)):
                row = summary[size][label]
                unit = 0.004 * factor * mult
                phases = row["profiled_ms_per_trace"]
                for index, phase in enumerate(PHASES, start=1):
                    self.assertAlmostEqual(phases[phase], index * unit)
                self.assertAlmostEqual(phases["residual"], 9 * unit)
                self.assertAlmostEqual(phases["total"], 37 * unit)
                self.assertAlmostEqual(row["control_ms_per_trace"], 36 * unit)
        report["trials"].pop()
        with self.assertRaisesRegex(ValueError, "all eight"):
            summarize(report)

    def test_phase_timers_must_fit_each_trace_and_capture_all_calls(self):
        writer = self.writer(1, True)
        # Trial-wide totals would still fit, but this first trace is over budget.
        writer["phase_samples"][0]["duration_ns"]["commit"] += 10000
        with self.assertRaisesRegex(ValueError, "exceed"):
            phase_totals(writer, 2)
        writer = self.writer(1, True)
        writer["phase_samples"][0]["calls"]["commit"] = 1
        with self.assertRaisesRegex(ValueError, "call counts"):
            phase_totals(writer, 2)
        writer["phase_samples"].pop()
        with self.assertRaisesRegex(ValueError, "entire"):
            phase_totals(writer, 2)


if __name__ == "__main__":
    unittest.main()
