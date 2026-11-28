"""Compare SQLAlchemy result-processing timers on matched archival queues."""

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
CONSUMPTION_MODES = {
    "control": {"profile_result_detail": True},
    "detail": {"profile_consumption": True},
}


def compare(rounds=7, traces=92, *, study="results"):
    if rounds < 1 or traces < 1:
        raise ValueError("rounds and traces must be positive")
    modes = {"results": MODES, "consumption": CONSUMPTION_MODES}[study]
    reports = {}
    # Cycle through every execution order. Each invocation seeds a fresh database.
    for number, order in zip(range(1, rounds + 1), cycle(permutations(modes))):
        reference = None
        for mode in order:
            sample = run(1, traces, **modes[mode])
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
    parser.add_argument("--study", choices=("results", "consumption"), default="results",
                        help="compare result-processing timers or the operations inside row consumption")
    parser.add_argument("--rounds", type=positive_int, default=7, help="fresh queues per mode")
    parser.add_argument("--traces", type=positive_int, default=92, help="two-span traces per queue")
    parser.add_argument("--output-dir", type=Path, help="directory for the per-mode JSON.gz reports")
    args = parser.parse_args()
    reports = compare(args.rounds, args.traces, study=args.study)
    output_dir = args.output_dir or Path("row-consumption" if args.study == "consumption" else "result-timing")
    output_dir.mkdir(parents=True, exist_ok=True)
    for mode, report in reports.items():
        path = output_dir / f"{mode}.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as out:
            json.dump(report, out, indent=2)
            out.write("\n")
        group = report["summary"][0]
        if args.study == "consumption":
            print(f"{mode} ({group['passes']} passes of {group['traces_per_pass']}): "
                  f"mean row consumption {group['mean_result_detail_ms']['consumption']:.3f} ms. Saved {path}.")
            for name, ms in group.get("mean_consumption_ms", {}).items():
                print(f"  {name}: {ms:.3f} ms")
            continue
        print(f"{mode} ({group['passes']} passes of {group['traces_per_pass']}): "
              f"mean result processing {group['mean_result_ms']['results']:.3f} ms, "
              f"mean remainder {group['mean_result_ms']['other']:.3f} ms. Saved {path}.")


if __name__ == "__main__":
    main()
