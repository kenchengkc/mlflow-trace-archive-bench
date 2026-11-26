"""Measure MLflow's experiment-chunk finder while a separate process commits new traces."""
import argparse
from bisect import bisect_left
from contextlib import contextmanager, ExitStack
import gzip
from importlib.metadata import version
import json
import multiprocessing
from pathlib import Path
import platform
from statistics import median
from time import perf_counter_ns
import traceback

from benchmark import positive_int, postgres_settings
from chunk_fixture import BATCH_SIZE, CUTOFF, prepare_chunk_fixture
from chunk_ingest import payload_text, verify_ingested, write_trace
from chunk_study import candidate_rows, verify_scope
from local_postgres import local_database
from mlflow_driver import MLFLOW_REVISION, find_candidates, open_store

MODES = ("idle_1000", "mixed_1001", "writer_only", "idle_1001", "mixed_1000")
SIZES = (1000, 1001)


def backend_pid(store):
    with store.ManagedSessionMaker() as session:
        return session.connection().connection.driver_connection.get_backend_pid()


def writer_worker(pipe):
    """Warm up before acknowledging readiness, and acknowledge each log_spans return locally."""
    from sqlalchemy import event

    try:
        while (job := pipe.recv()) is not None:
            uri, count, padding_bytes, profiled = job
            # Generating the deterministic attribute text is fixture setup. Span
            # construction, serialization and log_spans remain inside the timer.
            if padding_bytes:
                payloads = [payload_text(1, index, padding_bytes) for index in range(count)]
            else:
                payloads = [None] * count
            with open_store(uri) as store:
                store.engine.dispose()
                pids = set()

                def record_backend(connection, record):
                    pids.add(connection.get_backend_pid())

                # MLflow may use multiple physical connections within log_spans.
                # Track pool creation on all trials.
                event.listen(store.engine, "connect", record_backend)
                with ExitStack() as stack:
                    stack.callback(event.remove, store.engine, "connect", record_backend)
                    recorder = None
                    if profiled:
                        from ingest_phases import capture_writer_phases
                        recorder = stack.enter_context(capture_writer_phases(store.engine))
                    warm_payload = payload_text(2, 0, padding_bytes) if padding_bytes else None
                    write_trace(store, 2, 0, warm_payload)
                    pipe.send(("ready", sorted(pids)))
                    if pipe.recv() != "go":
                        raise RuntimeError("Writer did not receive its start signal")
                    acknowledged = []
                    started = perf_counter_ns()
                    if recorder is None:
                        for index, payload in enumerate(payloads):
                            write_trace(store, 1, index, payload)
                            acknowledged.append(perf_counter_ns())
                    else:
                        for index, payload in enumerate(payloads):
                            with recorder.trace():
                                write_trace(store, 1, index, payload)
                            acknowledged.append(perf_counter_ns())
            samples = recorder.samples if recorder is not None else None
            pipe.send(("done", (started, acknowledged, sorted(pids), samples)))
    except Exception:
        pipe.send(("error", traceback.format_exc()))
    finally:
        pipe.close()


@contextmanager
def writer_process():
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=writer_worker, args=(child,))
    process.start()
    child.close()
    try:
        yield parent
        parent.send(None)
    except BaseException:
        process.terminate()
        raise
    finally:
        parent.close()
        process.join()
    if process.exitcode:
        raise RuntimeError(f"Writer process exited with code {process.exitcode}")


def receive(pipe, expected):
    status, payload = pipe.recv()
    if status != expected:
        raise RuntimeError(f"Expected writer {expected}, received {status}: {payload}")
    return payload


def select_once(store, ids, expected, pid, start_writer=None):
    started = perf_counter_ns()
    with store.ManagedSessionMaker() as session:
        current_pid = session.connection().connection.driver_connection.get_backend_pid()
        if start_writer is not None:
            start_writer.send("go")
        rows = find_candidates(store, session, ids, CUTOFF, BATCH_SIZE)
    finished = perf_counter_ns()
    if current_pid != pid or candidate_rows(rows) != expected:
        raise RuntimeError("Finder backend changed or merged batch differs from the global sort")
    return started, finished


def db_sql(sql, name):
    def execute(statement, *, variables=None):
        return sql(f"\\connect {name}\n" + statement, variables=variables)
    return execute


def measure_trial(uri, sql, pipe, mode, scopes, expected, traces, idle_calls, padding_bytes=None,
                  profiled=None, *, upsert_probe=None):
    size = None if mode == "writer_only" else int(mode.split("_")[1])
    mixed = mode.startswith("mixed")
    count = traces if mixed or size is None else 0
    pipe.send((uri, count, padding_bytes, profiled))
    writer_pids = receive(pipe, "ready")
    with open_store(uri) as store, ExitStack() as probes:
        store.engine.dispose()
        reader_pid = backend_pid(store)
        if not writer_pids or reader_pid in writer_pids:
            raise RuntimeError("Finder and writer must use separate database backends")
        # Warm both finder paths on every clone, including the writer-only control.
        for n in SIZES:
            select_once(store, scopes[n], expected[n], reader_pid)
        if upsert_probe is not None:
            # Both connections are warm. The writer is blocked until the first finder call.
            upsert = probes.enter_context(upsert_probe(sql, count, padding_bytes))
        intervals = []
        if mixed:
            # The release call straddles the start fence and is excluded from the median.
            intervals.append(select_once(store, scopes[size], expected[size], reader_pid, pipe))
            while not pipe.poll():
                intervals.append(select_once(store, scopes[size], expected[size], reader_pid))
        else:
            pipe.send("go")
            if size is not None:
                # Let the zero-write worker close its warmed session before timing idle calls.
                receive(pipe, "done")
                for _ in range(idle_calls):
                    intervals.append(select_once(store, scopes[size], expected[size], reader_pid))
        if count:
            started, commits, writer_pids, samples = receive(pipe, "done")
            if reader_pid in writer_pids:
                raise RuntimeError("Writer and finder shared a backend")
        else:
            started, commits = intervals[0][0], []
    stored = verify_ingested(sql, 1, count, payload_bytes=padding_bytes)
    if len(commits) != count:
        raise RuntimeError("Writer acknowledgement count differs from the requested workload")
    result = {"mode": mode, "writer": None, "finder": None}
    if upsert_probe is not None:
        result["upsert"] = upsert
    if profiled is not None:
        result["profiled"] = profiled
    if count:
        duration = commits[-1] - started
        result["writer"] = {"backend_pids": writer_pids, "elapsed_ns": duration,
                            "commit_offsets_ns": [t - started for t in commits],
                            "traces_per_second": count * 1e9 / duration, **stored}
        if profiled:
            result["writer"]["phase_samples"] = samples
    if size is not None:
        measured = [(a, b) for a, b in intervals if not mixed or started <= a <= b <= commits[-1]]
        if not measured:
            raise RuntimeError("No complete finder transaction in the writer interval; increase --traces")
        overlaps = sum(bisect_left(commits, b) - bisect_left(commits, a) for a, b in measured)
        if mixed and not overlaps:
            raise RuntimeError("No writer commit acknowledgment inside a measured selection; increase --traces")
        result["finder"] = {
            "backend_pid": reader_pid, "experiments": size, "verified_calls": len(intervals),
            "measured_calls": len(measured), "median_ms": median((b-a) / 1e6 for a, b in measured),
            "commit_acks_during_measured_calls": overlaps,
            "transaction_offsets_ns": [[a - started, b - started] for a, b in intervals],
        }
    return result


def run_trials(schedule, traces, idle_calls, study_settings, *, record_durability=False,
               upsert_probe=None):
    """Give every scheduled trial a fresh clone of the same verified fixture."""
    from sqlalchemy.engine import make_url

    if traces < 2:
        raise ValueError("Use at least two ingested traces per writer trial")
    scopes = {size: list(map(str, range(1, size+1))) for size in SIZES}
    with local_database(preload_pg_stat_statements=upsert_probe is not None) as (sql, uri), writer_process() as pipe:
        url = make_url(uri)
        sql("CREATE DATABASE ingest_seed")
        seed_sql = db_sql(sql, "ingest_seed")
        if upsert_probe is not None:
            from upsert_server import prepare_server_stats
            extension_version = prepare_server_stats(seed_sql)
        with open_store(url.set(database="ingest_seed").render_as_string(hide_password=False)) as store:
            fixture = prepare_chunk_fixture(seed_sql)
            checks = {n: verify_scope(store, ids) for n, ids in scopes.items()}
        expected = {n: c["ordered_rows"] for n, c in checks.items()}
        report = {
            "mlflow_revision": MLFLOW_REVISION, "packages": {p: version(p) for p in
            ("mlflow", "SQLAlchemy", "psycopg2-binary")}, "python": platform.python_version(),
            "system": platform.system(), "architecture": platform.machine(), "fixture": fixture,
            "postgres_settings": postgres_settings(seed_sql),
            "alembic_revision": seed_sql("SELECT version_num FROM alembic_version"),
            **study_settings, "traces_per_writer_trial": traces,
            "cutoff_ms": CUTOFF, "batch_size": BATCH_SIZE,
            "scopes": {n: {k: c[k] for k in ("query_count", "result_sha256", "first_excluded", "ordered_rows")}
                       for n, c in checks.items()}, "trials": [],
        }
        if record_durability:
            report["postgres_settings"].update(json.loads(seed_sql("""
                SELECT json_object_agg(name, current_setting(name)) FROM pg_settings
                WHERE name IN ('fsync', 'synchronous_commit', 'full_page_writes', 'wal_sync_method');
            """)))
        if upsert_probe is not None:
            report["postgres_settings"].update(json.loads(seed_sql("""
                SELECT json_object_agg(name, current_setting(name)) FROM pg_settings
                WHERE name IN ('wal_level', 'wal_compression', 'default_toast_compression', 'compute_query_id',
                               'pg_stat_statements.track', 'pg_stat_statements.track_planning');
            """)))
            report["pg_stat_statements_version"] = extension_version
        trial_uri = url.set(database="ingest_trial").render_as_string(hide_password=False)
        for round_no, mode, padding_bytes, *profile_option in schedule:
            sql("CREATE DATABASE ingest_trial TEMPLATE ingest_seed")
            trial = measure_trial(trial_uri, db_sql(sql, "ingest_trial"), pipe, mode, scopes,
                                  expected, traces, idle_calls, padding_bytes, *profile_option,
                                  upsert_probe=upsert_probe)
            if padding_bytes is not None:
                trial["padding_bytes"] = padding_bytes
            report["trials"].append({"round": round_no, **trial})
            sql("DROP DATABASE ingest_trial")
    return report


def run(rounds=5, traces=512, idle_calls=256):
    if min(rounds, idle_calls) < 1:
        raise ValueError("Use positive rounds and idle calls")
    schedule = []
    for round_no in range(rounds):
        offset = round_no % len(MODES)
        schedule.extend((round_no + 1, mode, None) for mode in MODES[offset:] + MODES[:offset])
    return run_trials(schedule, traces, idle_calls,
                      {"round_count": rounds, "idle_calls_per_trial": idle_calls})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=positive_int, default=5, help="five rounds balance trial positions")
    parser.add_argument("--traces", type=positive_int, default=512, help="single-trace commits per writer trial")
    parser.add_argument("--idle-calls", type=positive_int, default=256)
    parser.add_argument("--output", type=Path, help="save trial rates, transaction intervals and commit times")
    args = parser.parse_args()
    report = run(args.rounds, args.traces, args.idle_calls)
    print("| Workload | Finder latency (ms) | Ingested traces/s |")
    print("| --- | ---: | ---: |")
    for mode in MODES:
        trials = [t for t in report["trials"] if t["mode"] == mode]
        read = [t["finder"]["median_ms"] for t in trials if t["finder"]]
        write = [t["writer"]["traces_per_second"] for t in trials if t["writer"]]
        print(f"| {mode} | {f'{median(read):.2f}' if read else '—'} | "
              f"{f'{median(write):.2f}' if write else '—'} |")
    if args.output:
        opener = gzip.open if args.output.suffix == ".gz" else open
        with opener(args.output, "wt", encoding="utf-8") as target:
            json.dump(report, target)
            target.write("\n")


if __name__ == "__main__":
    main()
