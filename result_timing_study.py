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
TAG_LOAD_MODES = {
    "control": {"profile_consumption": True},
    "detail": {"profile_tag_load": True},
}
TAG_PREP_MODES = {
    "control": {"profile_tag_load": True},
    "detail": {"profile_tag_prep": True},
}
TAG_SELECT_MODES = {
    "control": {"profile_tag_prep": True},
    "detail": {"profile_tag_select": True},
}
STUDIES = {"results": MODES, "consumption": CONSUMPTION_MODES,
           "tag-load": TAG_LOAD_MODES, "tag-prep": TAG_PREP_MODES, "tag-select": TAG_SELECT_MODES}
BREAKDOWNS = {
    "consumption": ("mean_result_detail_ms", "consumption", "row consumption", "mean_consumption_ms"),
    "tag-load": ("mean_consumption_ms", "eager_tags", "exclusive eager tag loading", "mean_tag_load_ms"),
    "tag-prep": ("mean_tag_load_ms", "preparation", "tag query preparation", "mean_tag_prep_ms"),
    "tag-select": ("mean_tag_prep_ms", "select_construction", "tag SELECT construction", "mean_tag_select_ms"),
}


def compare(rounds=7, traces=92, *, study="results"):
    if rounds < 1 or traces < 1:
        raise ValueError("rounds and traces must be positive")
    modes = STUDIES[study]
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
    parser.add_argument("--study", choices=STUDIES, default="results",
                        help="choose the operation to break down against its matched control")
    parser.add_argument("--rounds", type=positive_int, default=7, help="fresh queues per mode")
    parser.add_argument("--traces", type=positive_int, default=92, help="two-span traces per queue")
    parser.add_argument("--output-dir", type=Path, help="directory for the per-mode JSON.gz reports")
    args = parser.parse_args()
    reports = compare(args.rounds, args.traces, study=args.study)
    directories = {"results": "result-timing", "consumption": "row-consumption"}
    output_dir = args.output_dir or Path(directories.get(args.study, args.study))
    output_dir.mkdir(parents=True, exist_ok=True)
    for mode, report in reports.items():
        path = output_dir / f"{mode}.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as out:
            json.dump(report, out, indent=2)
            out.write("\n")
        group = report["summary"][0]
        if args.study in BREAKDOWNS:
            parent, bucket, label, parts = BREAKDOWNS[args.study]
            parent_ms = group[parent][bucket]
            print(f"{mode} ({group['passes']} passes of {group['traces_per_pass']}): "
                  f"mean {label} {parent_ms:.3f} ms. Saved {path}.")
            for name, ms in group.get(parts, {}).items():
                print(f"  {name}: {ms:.3f} ms")
            continue
        print(f"{mode} ({group['passes']} passes of {group['traces_per_pass']}): "
              f"mean result processing {group['mean_result_ms']['results']:.3f} ms, "
              f"mean remainder {group['mean_result_ms']['other']:.3f} ms. Saved {path}.")


if __name__ == "__main__":
    main()
