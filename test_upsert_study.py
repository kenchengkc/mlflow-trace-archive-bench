import unittest
from collections import Counter
from itertools import groupby, permutations

from ingest_phases import PHASES
from phase_study import EXPECTED_CALLS, LARGE_BYTES
from upsert_study import CONDITIONS, summarize, trial_schedule


class UpsertStudyTest(unittest.TestCase):
    def test_schedule_balances_position_and_predecessor_over_eight_rounds(self):
        positions = [Counter() for _ in CONDITIONS]
        transitions = Counter()
        for _, group in groupby(trial_schedule(8), lambda t: t[0]):
            rows = list(group)
            self.assertTrue(all(row[3] for row in rows))
            order = [row[1:3] for row in rows]
            self.assertCountEqual(order, CONDITIONS)
            for position, condition in zip(positions, order):
                position[condition] += 1
            transitions.update(zip(order, order[1:]))
        for position in positions:
            self.assertEqual(position, {arm: 2 for arm in CONDITIONS})
        self.assertEqual(transitions, {pair: 2 for pair in permutations(CONDITIONS, 2)})

    @staticmethod
    def report():
        count = 2
        trials = []
        for r, mode, padding, profiled in trial_schedule(2):
            # The second scope and second round have independently different costs.
            factor = r + (mode == "mixed_1001") + 2 * bool(padding)
            durations = dict.fromkeys(PHASES, 1)
            durations["span_write"] = factor * 1_000_000
            trace_ns = sum(durations.values()) + 100
            writer = {"phase_samples": [{"calls": EXPECTED_CALLS, "duration_ns": durations}] * count,
                      "elapsed_ns": trace_ns * count, "commit_offsets_ns": [trace_ns, trace_ns * count],
                      "payload": {"stored_bytes_sum": (padding or 399) * count}}
            statement = {"calls": count, "rows": count, "total_exec_time_ms": factor / 4 * count,
                         "wal_bytes": (padding + r) * count, "wal_records": 2 * count, "wal_fpi": 1}
            toast = {"external_values": count if padding else 0, "chunks": 5 * bool(padding) * count,
                     "chunk_bytes": padding * count, "heap_bytes": padding * count, "index_bytes": 0}
            trials.append({"round": r, "mode": mode, "padding_bytes": padding, "profiled": profiled,
                           "writer": writer, "upsert": {"statement": {"delta": statement},
                                                       "toast": {"delta": toast}}})
        return {"trials": trials, "round_count": 2, "traces_per_writer_trial": count}

    def test_means_use_matched_calls_in_both_rounds_and_do_not_double_count_server_time(self):
        groups = summarize(self.report())
        for size, baseline in (("1000", 1.5), ("1001", 2.5)):
            for name, factor in (("small", baseline), ("64_kib", baseline + 2), ("delta", 2)):
                row = groups[size][name]
                self.assertEqual(row["driver_ms"], factor)
                self.assertEqual(row["execution_ms"], factor / 4)
                self.assertEqual(row["gap_ms"], factor * 3 / 4)
            self.assertEqual(groups[size]["small"]["wal_bytes"], 1.5)
            self.assertEqual(groups[size]["delta"]["wal_bytes"], LARGE_BYTES)
            self.assertEqual(groups[size]["delta"]["toast_chunk_bytes"], LARGE_BYTES)

    def test_rejects_misaligned_driver_server_or_storage_samples_and_incomplete_round(self):
        report = self.report()
        trial = next(t for t in report["trials"] if t["padding_bytes"])
        stmt = trial["upsert"]["statement"]["delta"]
        stmt["calls"] += 1
        with self.assertRaisesRegex(ValueError, "exactly one"):
            summarize(report)
        stmt["calls"] -= 1
        trial["upsert"]["toast"]["delta"]["chunk_bytes"] -= 1
        with self.assertRaisesRegex(ValueError, "chunk bytes"):
            summarize(report)
        trial["upsert"]["toast"]["delta"]["chunk_bytes"] += 1
        report["trials"].pop()
        with self.assertRaisesRegex(ValueError, "all four"):
            summarize(report)


if __name__ == "__main__":
    unittest.main()
