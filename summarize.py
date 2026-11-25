"""Summarize a three-strategy MLflow trace archival study as a Markdown table."""

import argparse
import gzip
import json
from pathlib import Path
from statistics import median


STRATEGIES = {
    "global": "Global",
    "per_experiment": "Per experiment",
    "bounded_probe": "Bounded probe",
}


def include_planning(measurement: dict) -> dict:
    execution = measurement["samples_ms"]
    planning = measurement.get("planning_samples_ms", [])
    if not execution or len(planning) != len(execution):
        raise ValueError("--with-planning requires one planning time for every execution sample")
    totals = [plan_ms + run_ms for plan_ms, run_ms in zip(planning, execution)]
    return {**measurement, "median_ms": median(totals), "samples_ms": totals}


def fastest_label(measurements: dict) -> str:
    # Halfway medians of millisecond readings can differ by a floating-point rounding bit.
    medians = {name: round(measurements[name]["median_ms"], 4) for name in STRATEGIES}
    best = min(medians.values())
    winners = [name for name, median in medians.items() if median == best]
    if len(winners) > 1:
        return " = ".join(STRATEGIES[name] for name in winners)

    winner = winners[0]
    samples = measurements[winner]["samples_ms"]
    if not samples:
        raise ValueError("missing timing samples")
    for name, median in medians.items():
        if name == winner:
            continue
        other = measurements[name]["samples_ms"]
        if not other:
            raise ValueError("missing timing samples")
        if min(len(samples), len(other)) < 2 or median <= best * 1.05 or min(other) <= max(samples):
            return STRATEGIES[winner] + "†"
    return STRATEGIES[winner]


def experiment_label(ids: list[int]) -> str:
    ids = sorted(ids)
    if len(ids) > 2 and all(b == a + 1 for a, b in zip(ids, ids[1:])):
        return f"{ids[0]}–{ids[-1]}"
    return ", ".join(map(str, ids)) or "∅"


def summary(report: dict, buffers: bool, with_planning: bool = False) -> str:
    cases = report["cases"]
    if not cases or any("strategies" not in case for case in cases):
        raise ValueError("expected a study report with three strategy measurements per case")

    version = report["postgres_settings"]["server_version"]
    timing = "Times are median server execution times in milliseconds."
    if with_planning:
        timing = "Times are median milliseconds after adding server planning and execution time for each sample."
    lines = [f"PostgreSQL {version}. {timing}"]
    if buffers:
        lines.append(
            "Each strategy cell shows milliseconds / shared-buffer accesses. The access count "
            "adds hits and reads at the root of the representative execution plan and counts repeated visits."
        )
    columns = ["Experiment IDs", "Trace rows", "Batch", "Archived", "Returned",
               *STRATEGIES.values(), "Lowest median"]
    lines.extend(["", "| " + " | ".join(columns) + " |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"])

    def sort_key(case):
        ids = case["experiment_ids"]
        return len(ids), sorted(ids), case["rows"], case["batch_size"], case["archived_pct"]

    for case in sorted(cases, key=sort_key):
        measurements = case["strategies"]
        if with_planning:
            measurements = {name: include_planning(measurements[name]) for name in STRATEGIES}
        cells = [
            experiment_label(case["experiment_ids"]), f"{case['rows']:,}", f"{case['batch_size']:,}",
            f"{case['archived_pct']}%", f"{case['returned']:,}",
        ]
        for name in STRATEGIES:
            measurement = measurements[name]
            # PostgreSQL reports milliseconds to three decimals; an even sample count can add a fourth.
            cell = f"{measurement['median_ms']:,.4f}".rstrip("0").rstrip(".")
            if buffers:
                plan = measurement["representative_plan"]["Plan"]
                accesses = plan["Shared Hit Blocks"] + plan["Shared Read Blocks"]
                cell += f" / {accesses:,}"
            cells.append(cell)
        cells.append(fastest_label(measurements))
        lines.append("| " + " | ".join(cells) + " |")

    lines.extend(["", "† marks a gap within 5% of the lowest median, overlapping observed timing "
                  "ranges, or fewer than two samples for a comparison. Equal medians share the label."])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, help="study results (.json or .json.gz)")
    parser.add_argument("--buffers", action="store_true",
                        help="include shared-buffer hits plus reads from each representative plan")
    parser.add_argument("--with-planning", action="store_true",
                        help="add each sample's planning time to its execution time before comparing medians")
    args = parser.parse_args()
    opener = gzip.open if args.results.suffix.lower() == ".gz" else open
    try:
        with opener(args.results, "rt", encoding="utf-8") as source:
            report = json.load(source)
        print(summary(report, args.buffers, args.with_planning))
    except (OSError, EOFError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"{parser.prog}: {args.results}: {exc}\n")


if __name__ == "__main__":
    main()
