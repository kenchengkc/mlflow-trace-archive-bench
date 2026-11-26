"""Compare driver and executor time, statement WAL and TOAST storage for live MLflow span upserts."""
import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import gzip
import json
from pathlib import Path

from benchmark import positive_int
from chunk_ingest_study import SIZES, run_trials
from payload_study import verify_payload_comparison
from phase_study import LARGE_BYTES, phase_totals


CONDITIONS = tuple((f"mixed_{size}", padding) for size in SIZES for padding in (0, LARGE_BYTES))


def trial_schedule(rounds):
    """Each condition occupies all four positions and follows every other one in four rounds."""
    for r in range(rounds):
        for i in (0, 1, 3, 2):
            yield r + 1, *CONDITIONS[(i + r) % 4], True


@contextmanager
def capture_upsert(sql, count, padding):
    from upsert_server import upsert_delta, upsert_snapshot
    from upsert_toast import toast_delta, toast_snapshot

    before_sql = upsert_snapshot(sql)
    before_toast = toast_snapshot(sql)
    observations = {}
    yield observations
    after_sql = upsert_snapshot(sql)
    after_toast = toast_snapshot(sql)
    if before_sql["dbid"] != before_toast["database_oid"] or after_sql["dbid"] != after_toast["database_oid"]:
        raise ValueError("Statement and TOAST snapshots came from different databases")
    observations["statement"] = {"before": before_sql, "after": after_sql,
                                 "delta": upsert_delta(before_sql, after_sql, count)}
    observations["toast"] = {"before": before_toast, "after": after_toast,
                             "delta": toast_delta(before_toast, after_toast, count, padding)}


def summarize(report):
    """Divide totals by the same number of real upserts for each treatment."""
    count, rounds = report["traces_per_writer_trial"], report["round_count"]
    totals = defaultdict(Counter)
    seen = set()
    for trial in report["trials"]:
        condition = trial["mode"], trial["padding_bytes"]
        entry = trial["round"], condition
        if condition not in CONDITIONS or not 1 <= trial["round"] <= rounds or entry in seen:
            raise ValueError("Unexpected upsert trial or duplicate condition in a round")
        seen.add(entry)
        stmt = trial["upsert"]["statement"]["delta"]
        toast = trial["upsert"]["toast"]["delta"]
        driver_ms = phase_totals(trial["writer"], count)["span_write"] / 1e6
        execution_ms = stmt["total_exec_time_ms"]
        if not trial["profiled"] or stmt["calls"] != count or stmt["rows"] != count:
            raise ValueError("Driver and server must cover exactly one span upsert per trace")
        content_bytes = trial["writer"]["payload"]["stored_bytes_sum"]
        if trial["padding_bytes"] == LARGE_BYTES and toast["chunk_bytes"] != content_bytes:
            raise ValueError("TOAST chunk bytes differ from the readback size of the large span contents")
        if not 0 <= execution_ms <= driver_ms:
            raise ValueError("Server execution time exceeds the enclosing driver time or is negative")
        totals[condition].update({
            "driver_ms": driver_ms, "execution_ms": execution_ms, "gap_ms": driver_ms - execution_ms,
            "wal_bytes": stmt["wal_bytes"], "wal_records": stmt["wal_records"], "wal_fpi": stmt["wal_fpi"],
            **{f"toast_{key}": toast[key] for key in
               ("external_values", "chunks", "chunk_bytes", "heap_bytes", "index_bytes")},
        })
    if len(seen) != rounds * len(CONDITIONS):
        raise ValueError("Each round requires all four upsert conditions")
    summary = {}
    for size in SIZES:
        scope = {}
        for label, padding in (("small", 0), ("64_kib", LARGE_BYTES)):
            group = totals[f"mixed_{size}", padding]
            scope[label] = {field: value / (rounds * count) for field, value in group.items()}
        scope["delta"] = {k: value - scope["small"][k] for k, value in scope["64_kib"].items()}
        summary[str(size)] = scope
    return summary


def run(rounds=8, traces=512):
    if rounds < 1:
        raise ValueError("Use at least one measurement round")
    settings = {
        "round_count": rounds, "padding_sizes_bytes": [0, LARGE_BYTES], "attribute_key": "benchmark.payload",
        "aggregation": "Trial totals divided by the total measured upserts in each condition. "
                       "The driver/server gap includes work outside PostgreSQL's executor.",
    }
    report = run_trials(trial_schedule(rounds), traces, idle_calls=0, study_settings=settings,
                        record_durability=True, upsert_probe=capture_upsert)
    verify_payload_comparison(report)
    report["summary"] = summarize(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=positive_int, default=8, help="multiples of four balance trial order")
    parser.add_argument("--traces", type=positive_int, default=512, help="single-span commits per trial")
    parser.add_argument("--output", type=Path, help="save raw phase times, server statistics and TOAST snapshots")
    args = parser.parse_args()
    report = run(args.rounds, args.traces)
    print("| Experiments | Payload | Driver (ms) | PostgreSQL execution (ms) | Difference (ms) "
          "| WAL bytes | TOAST chunks | TOAST chunk bytes |")
    print("| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for size, scope in report["summary"].items():
        for label, row in scope.items():
            print(f"| {size} | {label} | {row['driver_ms']:.3f} | {row['execution_ms']:.3f} | "
                  f"{row['gap_ms']:.3f} | {row['wal_bytes']:.1f} | {row['toast_chunks']:.1f} | "
                  f"{row['toast_chunk_bytes']:.1f} |")
    if args.output:
        opener = gzip.open if args.output.suffix == ".gz" else open
        with opener(args.output, "wt", encoding="utf-8") as target:
            json.dump(report, target)
            target.write("\n")


if __name__ == "__main__":
    main()
