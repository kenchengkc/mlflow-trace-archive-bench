# MLflow Trace Archival Benchmark

This benchmark measures how queue layout and concurrent writes affect MLflow trace archival on PostgreSQL and verifies that each query returns the oldest eligible traces in order.

## Quick start

With Git and Python 3.10+, inspect the published million-trace run using only the standard library.

```sh
git clone https://github.com/kenchengkc/mlflow-trace-archive-bench.git
cd mlflow-trace-archive-bench
python3 summarize.py results/strategy-study/million-late.json.gz --with-planning
```

The output compares three queries side by side. Add `--buffers` to see how many database pages each plan accessed, counting repeated visits.

## Reproduce the benchmark

For a first live run, install PostgreSQL on macOS or Linux and put `initdb`, `pg_ctl`, and `psql` on your `PATH`. Run as a non-root user. This command builds two 1,000-trace queues in a disposable local cluster, checks their ordered results, and prints the timings. [Installation commands](CONTRIBUTING.md#install-postgresql)

```sh
python3 matched_queues.py --rows 1000 --batch-size 3 --samples 2
```

For studies that call MLflow’s tracking store, install the pinned dependencies in a virtual environment. [Run the finder](results/finder/README.md#reproduce-these-studies) · [Run concurrent ingestion](results/ingestion/README.md#reproduce-the-studies)

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-driver.txt
```

## Results

MLflow added an index after an operator reported waiting minutes to select three traces. On our one-million-trace fixture, the indexed global query selected 100 traces in **1.24 ms** on a fresh queue. After the oldest 95% of eligible traces were archived, it took **1,315.50 ms**. A query that first checks the oldest 200 matching headers took **284.59 ms** on that late batch. [Operator report](https://github.com/mlflow/mlflow/issues/24895) · [Upstream fix](https://github.com/mlflow/mlflow/pull/24976)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="results/strategy-study/query-latency-dark.png">
  <img src="results/strategy-study/query-latency.png" alt="Time to select 100 traces. The global query was fastest on a fresh queue. The bounded probe was fastest after the oldest 95% had been archived.">
</picture>

Markers show median PostgreSQL planning and execution time across six runs. Whiskers show the observed range. [Exact timings and query plans](results/strategy-study/README.md#what-changed-at-one-million-traces)

## Study guides

| Guide | Selected results |
| --- | --- |
| [SQL query strategies](results/strategy-study/README.md) | <a name="two-queues-with-95-archived"></a>[Queue layout changed the fastest query at 95% archived](results/strategy-study/README.md#two-queues-with-95-archived) · [Frequent and rare experiments](results/strategy-study/README.md#how-experiment-scope-changes-the-result) |
| [MLflow’s candidate finder](results/finder/README.md) | <a name="through-mlflows-database-driver"></a>[Driver vs. raw SQL](results/finder/README.md#through-mlflows-database-driver) · <a name="deferring-the-fallback"></a>[Two-step selection](results/finder/README.md#deferring-the-fallback) · <a name="from-1000-to-1001-experiments"></a>[Experiment 1,001 triggered a second query](results/finder/README.md#from-1000-to-1001-experiments) |
| [Concurrent trace ingestion](results/ingestion/README.md) | <a name="selection-during-trace-ingestion"></a>[Selection during writes](results/ingestion/README.md#selection-during-trace-ingestion) · <a name="larger-incoming-spans"></a>[64 KiB spans cut write throughput by about 25%](results/ingestion/README.md#larger-incoming-spans) · <a name="where-the-64-kib-write-time-goes"></a>[Write timings](results/ingestion/README.md#where-the-64-kib-write-time-goes) · <a name="inside-the-span-upsert"></a>[Inside the span upsert](results/ingestion/README.md#inside-the-span-upsert) |

[Documentation home](https://kenchengkc.github.io/mlflow-trace-archive-bench/) · [Project roadmap](ROADMAP.md)

## Contributing

Run a database-free check, contribute an ordering test, or share a PostgreSQL result. The [contributor guide](CONTRIBUTING.md) covers setup, tests, and what to include in a performance report.

[![PostgreSQL benchmark CI](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/workflows/postgres.yml/badge.svg?branch=main)](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/workflows/postgres.yml)

## License

Copyright 2026 Ken Cheng. Distributed under the [Apache License 2.0](LICENSE).
