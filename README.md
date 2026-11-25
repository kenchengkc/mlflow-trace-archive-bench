# MLflow trace archival benchmark

How much work does PostgreSQL do to find the next 100 traces to archive? This benchmark runs MLflow’s candidate-selection query on synthetic tracking data. It compares the same rows before and after adding the index on `(timestamp_ms, request_id)` that MLflow shipped in August 2026.

An MLflow operator reported that asking PostgreSQL 16 for just three traces could take minutes. The operator showed that a new index removed a blocking sort. The merged fix included SQLite timings. This project makes the PostgreSQL comparison repeatable and tests how it changes as the oldest traces leave the queue. [Operator report](https://github.com/mlflow/mlflow/issues/24895) · [Merged index and SQLite benchmark](https://github.com/mlflow/mlflow/pull/24976)

## Why this matters to Databricks engineers

MLflow Tracing records what happens during an AI request, including individual model and tool calls. Databricks uses those traces for debugging, evaluation, and monitoring. The open-source MLflow tracking server supports a PostgreSQL backend. Its archival worker needs to find completed traces whose span data can move out of the tracking database. A slow selection query can delay each pass before the worker moves its first trace. The measurements here apply to that open-source SQL path. Databricks also offers managed trace storage in Unity Catalog. [Databricks tracing overview](https://docs.databricks.com/aws/en/mlflow3/genai/tracing/overview) · [MLflow candidate selection](https://github.com/mlflow/mlflow/blob/3128eb46928fab08500124e9e0cde6791cf845aa/mlflow/store/tracking/sqlalchemy_store.py)

## First measurement

Median server execution time for a batch of 100, measured on a 100,000-trace synthetic database with PostgreSQL 14.19 on macOS arm64. Each cell represents seven warm executions.

| Oldest eligible traces already archived | Without the archival index | With the archival index |
| ---: | ---: | ---: |
| 0% | 13.39 ms | 1.20 ms |
| 50% | 44.21 ms | 33.45 ms |
| 95% | 18.54 ms | 63.31 ms |

The representative plan with the index returned 129 rows from its trace index scan to find the first 100 candidates at 0% archived. At 95% archived, the same scan returned 85,489 rows into the downstream filters to find 100 candidates. That explains why removing the sort can help early in a queue while making the later batches more expensive on this fixture. [Raw timings, plans and settings](results/postgresql-14-macos.json)

## Run it

Use Python 3.10+ and PostgreSQL server tools (`initdb`, `pg_ctl`, `psql`) on macOS or Linux, as a regular OS user. Python uses only the standard library. Each case starts a disposable database that listens on a private Unix socket and is removed on exit.

```sh
git clone https://github.com/kenchengkc/mlflow-trace-archive-bench.git
cd mlflow-trace-archive-bench
python3 -m unittest -v
python3 benchmark.py --output benchmark-result.json
```

To replay the operator’s three-candidate batch size on a smaller fixture, run `python3 benchmark.py --rows 10000 --archived-pct 0 95 --batch-size 3`. On Debian or Ubuntu, versioned server binaries may need to be added to `PATH` (for example, `/usr/lib/postgresql/16/bin`).

## What the benchmark measures

- **Workload.** The default run seeds 10,000 and 100,000 traces. It finds the eligible population, then simulates archiving the oldest 0%, 50%, and 95% of that population. Archiving clears the span payloads and writes MLflow’s two location tags. For example, at 100,000 traces, 72,386 qualify initially and 68,766 of those have been archived in the 95% case.
- **Query.** For one eight-experiment batch, PostgreSQL must return the oldest qualifying rows, breaking timestamp ties by trace ID. The fixture includes in-progress traces, failed archival attempts, excluded experiments, and missing or empty spans. Both variants retain the preexisting experiment-and-timestamp index. The database planner chooses the access path. [Query](sql/candidates.sql) · [Schema](sql/schema.sql)
- **Timing.** Both variants run on the same seeded cluster for each case. The runner refreshes planner statistics, retrieves the ordered rows, runs one warm-up plan and records seven server execution times with `EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)`. The JSON includes every timing, the plan closest to the median, PostgreSQL settings, index size, and a digest of the ordered result. Index build time and artifact uploads fall outside the timed interval. [PostgreSQL EXPLAIN](https://www.postgresql.org/docs/16/sql-explain.html)
- **Correctness.** Every comparison requires identical rows in identical order and the expected row count. A separate hand-authored fixture checks exact IDs, including a tied timestamp and a trace at the inclusive retention cutoff. CI runs the tests and publishes the raw benchmark JSON on PostgreSQL 16. [Eligibility test](test_candidates.py) · [CI runs](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/workflows/postgres.yml)

## Contribution plan

The first result to investigate is the cost of walking past already-archived traces to reach a small batch. The next experiment varies how many experiments share the queue. A useful upstream deliverable would be a small benchmark or a query change backed by the same correctness fixtures, raw plans, and an explicit account of when it helps. [Milestones and acceptance criteria](ROADMAP.md)

## License

Copyright 2026 Ken Cheng. Distributed under Apache License 2.0, the license used by MLflow. The SQL fixture models the selection logic in MLflow revision `3128eb4`. This is an independent community project. [Project license](LICENSE) · [Upstream license](https://github.com/mlflow/mlflow/blob/3128eb46928fab08500124e9e0cde6791cf845aa/LICENSE.txt)
