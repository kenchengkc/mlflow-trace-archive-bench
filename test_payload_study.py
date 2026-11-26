import unittest
from collections import defaultdict
from itertools import groupby, permutations
from unittest.mock import patch

from payload_study import summarize, trial_schedule, verify_payload_comparison


MODES = ("writer_only", "mixed_1000", "mixed_1001")
SIZES = (0, 16 * 1024, 64 * 1024)


class PayloadStudyTest(unittest.TestCase):
    def test_six_rounds_balance_the_order_of_workloads_and_payloads(self):
        rounds = []
        workload_orders = []
        size_orders = defaultdict(list)
        for number, trials in groupby(trial_schedule(6), key=lambda row: row[0]):
            rounds.append(number)
            workloads = []
            for mode, group in groupby(trials, key=lambda row: row[1]):
                workloads.append(mode)
                size_orders[mode].append(tuple(size for _, _, size in group))
            workload_orders.append(tuple(workloads))

        self.assertEqual(rounds, list(range(1, 7)))
        self.assertCountEqual(workload_orders, permutations(MODES))
        for mode in MODES:
            with self.subTest(mode=mode):
                self.assertCountEqual(size_orders[mode], permutations(SIZES))

    def test_all_workloads_require_the_same_trace_count_and_base_content(self):
        report = {"traces_per_writer_trial": 2, "trials": []}
        for number, mode, size in trial_schedule(1):
            report["trials"].append({
                "round": number, "mode": mode, "padding_bytes": size,
                "writer": {"trace_headers": 2, "root_spans": 2, "payload": {
                    "attribute_bytes_per_span": size, "base_content_sha256": "a" * 64,
                }},
            })
        verify_payload_comparison(report)
        for trial in report["trials"]:
            writer = trial["writer"]
            for fields, change in (
                (writer, {"trace_headers": 1}), (writer, {"root_spans": 1}),
                (writer["payload"], {"base_content_sha256": "b" * 64}),
                (writer["payload"], {"attribute_bytes_per_span": trial["padding_bytes"] + 1}),
            ):
                with self.subTest(mode=trial["mode"], size=trial["padding_bytes"], change=change):
                    with patch.dict(fields, change), self.assertRaises(RuntimeError):
                        verify_payload_comparison(report)

    def test_each_workload_compares_its_trial_medians_to_its_own_small_spans(self):
        # Deliberately skew the rounds. Their means and paired percent changes
        # differ from the published ratio of per-condition medians.
        latencies = ((1, 4, 9), (9, 5, 3), (7, 3, 8))
        rates = ((100, 160, 250), (90, 280, 120), (70, 40, 90))
        trials = []
        for offset, mode in enumerate(MODES, start=1):
            for size, times, speeds in zip(SIZES, latencies, rates):
                for number, (latency, speed) in enumerate(zip(times, speeds), start=1):
                    trials.append({
                        "mode": mode, "round": number, "padding_bytes": size,
                        "finder": None if mode == "writer_only" else {
                            "median_ms": latency * offset,
                            "measured_calls": 100 if number == 3 else 1,
                        },
                        "writer": {"traces_per_second": speed * offset},
                    })

        rows = {(row["mode"], row["padding_bytes"]): row for row in summarize({"trials": trials})}
        self.assertEqual(len(rows), len(MODES) * len(SIZES))
        for offset, mode in enumerate(MODES, start=1):
            for size, time, speed, latency_change, rate_change in (
                (SIZES[0], 4, 160, 0, 0),
                (SIZES[1], 5, 120, 25, -25),
                (SIZES[2], 7, 70, 75, -56.25),
            ):
                with self.subTest(mode=mode, size=size):
                    row = rows[mode, size]
                    self.assertAlmostEqual(row["median_traces_per_second"], speed * offset)
                    self.assertAlmostEqual(row["throughput_change_pct"], rate_change)
                    if mode == "writer_only":
                        self.assertIsNone(row["median_finder_ms"])
                        self.assertIsNone(row["finder_change_pct"])
                    else:
                        self.assertAlmostEqual(row["median_finder_ms"], time * offset)
                        self.assertAlmostEqual(row["finder_change_pct"], latency_change)
