"""Compare the result timer, its three method groups, and timer bookkeeping on matched queues."""

import argparse
import gzip
import json
from itertools import cycle, permutations
from pathlib import Path

from benchmark import positive_int
from drain_study import run, summarize


MODES = {
    "control": {"profile_results": True},
    "detail": {"profile_result_detail": True},
    "overhead": {"profile_timer_overhead": True},
}


def compare(rounds=7, traces=92):
    if rounds < 1 or traces < 1:
        raise ValueError("rounds and traces must be positive")
    reports = {}
    # Balance runner order over six rounds. Each invocation seeds a fresh database.
    for number, order in zip(range(1, rounds + 1), cycle(permutations(MODES))):
        reference = None
        for mode in order:
            sample = run(1, traces, **MODES[mode])
            trial, = sample.pop("rounds")
            observation = ([row["selected_ids"] for row in trial["passes"]], trial["readback"])
            if reference is not None and observation != reference:
                raise RuntimeError(f"Ordering or archive readback differs in round {number}, mode {mode}")
            reference = observation
            if mode not in reports:
                sample.update(round_count=rounds, comparison_mode=mode, rounds=[])
                reports[mode] = sample
            trial.update(round=number, mode_order=list(order))
            reports[mode]["rounds"].append(trial)
    for report in reports.values():
        report["summary"] = summarize(report["rounds"])
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=positive_int, default=7, help="fresh queues per mode")
    parser.add_argument("--traces", type=positive_int, default=92, help="two-span traces per queue")
    parser.add_argument("--output-dir", type=Path, default=Path("result-timing"),
                        help="directory for control.json.gz, detail.json.gz and overhead.json.gz")
    args = parser.parse_args()
    reports = compare(args.rounds, args.traces)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for mode, report in reports.items():
        path = args.output_dir / f"{mode}.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as out:
            json.dump(report, out, indent=2)
            out.write("\n")
        group = report["summary"][0]
        print(f"{mode} ({group['passes']} passes of {group['traces_per_pass']}): "
              f"mean result processing {group['mean_result_ms']['results']:.3f} ms, "
              f"mean remainder {group['mean_result_ms']['other']:.3f} ms. Saved {path}.")


if __name__ == "__main__":
    main()
