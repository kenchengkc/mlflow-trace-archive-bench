# MLflow Trace Archival Benchmark

[![PostgreSQL benchmark CI](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/workflows/postgres.yml/badge.svg?branch=main)](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/workflows/postgres.yml)

A PostgreSQL benchmark for the query MLflow uses to choose its next batch of traces to archive. It compares MLflow’s finder with alternative queries and checks that each returns the same traces in the same order.

MLflow added an index on timestamp and trace ID after a user reported waiting minutes to select three traces. This project builds on that fix by measuring how queue layout and concurrent trace ingestion affect selection. [Operator report](https://github.com/mlflow/mlflow/issues/24895) · [Upstream fix](https://github.com/mlflow/mlflow/pull/24976)

## Results

On a synthetic queue of **one million traces**, MLflow’s global SQL query selected the oldest 100 eligible traces in **1.24 ms**. After the oldest 95% of eligible traces had been archived, the same query took **1,315.50 ms**. A bounded probe that checks the first 200 matching headers before falling back to a full query took **284.59 ms** on that late batch.

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

## Quick start

Inspect the published million-trace result with Git and Python 3.10+. The report reader uses only the standard library.

```sh
git clone https://github.com/kenchengkc/mlflow-trace-archive-bench.git
cd mlflow-trace-archive-bench
python3 summarize.py results/strategy-study/million-late.json.gz --with-planning --buffers
```

## Reproduce the benchmark

On macOS or Linux, put PostgreSQL’s `initdb`, `pg_ctl`, and `psql` on your `PATH` and run as a non-root user. The benchmark creates a temporary cluster on a private Unix socket and removes it on exit. [Run the SQL comparisons](results/strategy-study/README.md#replay) · [Run MLflow’s finder](results/finder/README.md#reproduce-these-studies) · [Run the ingestion studies](results/ingestion/README.md#reproduce-the-studies)

## Contributing

For query changes, include a test with the expected trace IDs in order and run `python3 -m unittest -v`. For performance findings, include the PostgreSQL version, command, and result JSON. [Correctness tests](test_strategies.py) · [Project roadmap](ROADMAP.md)

## License

Copyright 2026 Ken Cheng. Distributed under the [Apache License 2.0](LICENSE).
