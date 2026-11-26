# MLflow Trace Archival Benchmark

[![PostgreSQL benchmark CI](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/workflows/postgres.yml/badge.svg?branch=main)](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/workflows/postgres.yml)

A PostgreSQL benchmark for the query MLflow uses to choose its next batch of traces to archive. It compares MLflow’s finder with alternative queries and checks that each returns the same traces in the same order.

MLflow added an index on timestamp and trace ID after a user reported waiting minutes to select three traces. This project builds on that fix by measuring what happens as the oldest traces leave the queue. [Operator report](https://github.com/mlflow/mlflow/issues/24895) · [Upstream fix](https://github.com/mlflow/mlflow/pull/24976)

## Results

On **one million synthetic traces**, MLflow’s global query selected the first batch of 100 in **1.24 ms**. After the oldest 95% of eligible traces had been archived, the same query took **1,315.50 ms**. An alternative that checks a short prefix of the queue took **284.59 ms** for that late batch.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="results/strategy-study/query-latency-dark.png">
  <img src="results/strategy-study/query-latency.png" alt="Time to select 100 traces across eight experiments. The global query was fastest at 0% archived and the bounded probe was fastest at 95% archived. Exact times and observed ranges follow.">
</picture>

Markers show median planning and execution time. Whiskers span the fastest and slowest of six runs. Both workloads used PostgreSQL 16.16, all eight experiments, and the same indexes. [Fresh-queue samples and plan](results/strategy-study/million-start.json.gz) · [Late-queue samples and plan](results/strategy-study/million-late.json.gz) · [How the queries work](results/strategy-study/README.md#three-queries-identical-answers)

<details>
<summary>Exact timings and observed ranges</summary>

Each cell gives the median followed by the minimum and maximum of six measured runs, in milliseconds.

| Query | Fresh queue | 95% archived |
| --- | ---: | ---: |
| Global (MLflow) | **1.24** (1.20–1.33) | 1,315.50 (1,311.40–1,331.15) |
| Per-experiment merge | 4.51 (4.38–4.69) | 1,848.73 (1,804.38–1,865.08) |
| Bounded probe | 27.95 (27.09–30.26) | **284.59** (282.93–287.06) |

</details>

At 95% archived, the global query passed 853,678 trace headers to its eligibility checks. The bounded probe checked 200 headers, then switched to filtering the full set. On the fresh queue, PostgreSQL’s just-in-time compilation accounted for most of the probe’s overhead. [Execution plans and controlled experiment](results/strategy-study/README.md#why-the-first-batch-was-slow)

### Two queues with 95% archived

**Changing which traces were archived flipped the fastest query.** Both fixtures started with identical headers for 100,000 traces. Of 72,386 initially eligible traces in the eight selected experiments, each fixture archived 68,766 and left 3,620. One archived the oldest traces first. The other used a fixed seed to scatter archival through time, matching the archived counts for each experiment and span count.

| Archive layout | Position of 100th eligible trace | Global (ms) | Per experiment (ms) | Bounded probe (ms) |
| --- | ---: | ---: | ---: | ---: |
| Oldest first | 85,489 | 142.10 | 154.00 | **36.27** |
| Scattered | 2,422 | **5.26** | 42.07 | 37.23 |

Both global queries used the same timestamp and trace-ID index. The oldest-first scan passed **35× as many headers** matching the experiments, cutoff and status to the eligibility checks. The global plan recorded 369,267 shared-buffer accesses on that queue and 11,062 on the scattered queue. The bounded probe found zero and seven eligible traces, respectively, in its first 200 headers, so it ran the full fallback in both layouts.

Times are medians of six warm PostgreSQL 16.16 runs per query and include database planning and execution. Buffer counts come from one representative plan per layout and include repeated accesses. All three queries returned the independently verified ordered batch for each fixture. [Raw samples and plans](results/strategy-study/matched-queues.json.gz) · [PostgreSQL run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45667039308)

### Through MLflow’s database driver

MLflow 3.18.0 took **112.18 ms** to select 100 traces from the oldest-first queue and **4.84 ms** from the equally archived scattered queue. We ran its actual SQLAlchemy candidate finder on three 100,000-trace databases initialized by MLflow’s migrations. Every timed call returned the expected trace IDs, experiment IDs and timestamps in order, with the same result digest as the earlier raw SQL study. [Upstream finder](https://github.com/mlflow/mlflow/blob/ba41106d110b67096384da12b6b85473457598db/mlflow/store/tracking/sqlalchemy_store.py#L7151-L7230)

| Queue | MLflow finder (ms) | Raw global SQL (ms) | Replay of MLflow’s SQL (ms) |
| --- | ---: | ---: | ---: |
| Fresh | 1.70 | 0.94 | 0.95 |
| 95% archived, oldest first | 112.18 | 111.20 | 110.70 |
| 95% archived, scattered | 4.84 | 4.05 | 4.04 |

The raw column runs this repository’s global query directly. The replay runs the exact SQL and bound parameters captured from MLflow. Each value is the median of 12 caller-side measurements on PostgreSQL 16.16, from opening the managed session through fetching the batch, committing and closing the session. The methods shared one warm psycopg2 connection per queue and ran in all six possible orders. The finder’s median was 0.76 ms above raw SQL on the fresh queue and 0.98 ms above it on the oldest-first queue. [Timing samples, SQL and plans](results/driver-study.json.gz) · [Benchmark run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45670762246)

The earlier reduced-schema study measured server planning and execution alone. Its fresh-queue plan scanned 30,882 tag rows looking for a span-location tag. MLflow’s migration puts the tag key first in the primary index, while the reduced fixture puts the trace ID first. Both migrated plans used that index for the lookup and found zero matching tags. [Earlier plan](results/strategy-study/matrix.json.gz) · [MLflow migration](https://github.com/mlflow/mlflow/blob/ba41106d110b67096384da12b6b85473457598db/mlflow/store/db_migrations/versions/867495a8f9d4_add_trace_tables.py#L57) · [Reduced schema](sql/schema.sql)

### Deferring the fallback

For a batch of 100, the two-step selector checks up to 200 of the oldest matching headers. If it fills the batch or finds fewer than 200 headers, it returns the eligible traces in order. Otherwise, it sends a second query that recomputes the entire batch. Both reads share a short, read-only, repeatable-read transaction. [Selector](two_step.py) · [Prefix query](sql/two_step_prefix.sql) · [Fallback query](sql/two_step_fallback.sql)

| Queue, 100,000 traces | MLflow finder (ms) | Single-statement probe (ms) | Two-step (ms) | Two-step fallback calls |
| --- | ---: | ---: | ---: | ---: |
| Fresh | 1.84 | 1.49 | **1.15** | 0/12 |
| 95% archived, oldest first | 112.71 | 28.46 | 28.96 | 12/12 |
| 95% archived, scattered | **5.36** | 27.72 | 27.41 | 12/12 |

Times are medians of 12 complete selection transactions per method, including checkout, isolation setup and commit on the same warm connection per queue. Every call returned the independently verified ordered batch. The two probes’ timing ranges overlapped within each archived layout. With archival scattered, two-step selection took **5.1× as long as MLflow’s finder**. For the rare experiment alone, all three medians were within 0.5 ms of one another on both archived queues. [Samples and plans](results/two-step.json.gz) · [Rare experiment](results/two-step-rare.json.gz) · [PostgreSQL 16.16 run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45672585264)

The largest fresh-queue difference appeared at **one million traces**. The single-statement probe took **14.79 ms** and its plan showed 67 just-in-time compilation functions, even though the fallback executed zero times. The two-step selector sent only the prefix query, whose plan showed no compilation, and took **1.40 ms**. MLflow’s finder took **2.10 ms**. [Million-trace samples and plans](results/two-step-million-fresh.json.gz)

To verify the order under concurrent writes, a test commits two changes on a second PostgreSQL connection between the probe and fallback. An older trace becomes eligible and a tied candidate is archived. The fallback returns the exact pre-commit batch, including both tied IDs in order. A new transaction sees both changes and agrees with MLflow’s finder. [Concurrency test](test_two_step.py)

### From 1,000 to 1,001 experiments

Adding experiment 1,001 to the selection triggered a second candidate query and increased median batch-selection time by **0.90 ms** on this fixture. We asked MLflow 3.18.0 for the oldest 100 traces on the same migrated PostgreSQL 16.16 database, with 101 eligible traces in each of 1,001 experiments. [MLflow’s finder and merge](https://github.com/mlflow/mlflow/blob/ba41106d110b67096384da12b6b85473457598db/mlflow/store/tracking/sqlalchemy_store.py#L7112-L7230)

| Selected experiments | Candidate SELECTs | Rows returned per SELECT | Rows kept per chunk | Median ms (min–max) |
| ---: | ---: | --- | --- | ---: |
| 1,000 | 1 | 100 | 100 | 2.68 (2.64–2.72) |
| 1,001 | 2 | 100 + 100 | 50 + 50 | 3.58 (3.52–3.73) |

Experiments 1,000 and 1,001 had traces with identical timestamps on either side of the chunk boundary. At the batch cutoff, trace ID `…0062` from experiment 1,000 ranked 100th and `…0063` from experiment 1,001 ranked 101st at the same timestamp. Every timed batch matched an independent global sort by timestamp and trace ID. Swapping the two experiments between chunks also preserved the exact order. [Fixture](chunk_fixture.py) · [Ordering test](test_chunk_study.py)

Timings cover 12 complete transactions per scope on one warm connection, alternating which scope ran first. The untimed query captures and replays verified the IDs, limits and row counts for each SELECT. [Raw samples, ordered rows and plans](results/chunk-study.json.gz) · [Benchmark run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45676900586)

### Selection during trace ingestion

A separate writer process committed one completed, single-span trace per call to MLflow’s `log_spans`, alternating between experiments 1,000 and 1,001. Incoming timestamps were newer than the archival cutoff. The expected oldest 100 traces therefore stayed fixed while the finder and writer used the same database. [Writer](chunk_ingest.py) · [MLflow ingestion path](https://github.com/mlflow/mlflow/blob/ba41106d110b67096384da12b6b85473457598db/mlflow/store/tracking/sqlalchemy_store.py#L5802-L5823)

With both processes active, **selection took about 16% longer and ingestion throughput fell about 13%** at both experiment counts. The writer alone committed **126.95 traces/s**. Percentage changes use the finder-only and writer-only controls measured in this run.

| Selected experiments | Finder alone (ms) | Finder with writer (ms) | Writer with finder (traces/s) |
| ---: | ---: | ---: | ---: |
| 1,000 | 4.98 | 5.77 (+15.8%) | 110.20 (−13.2%) |
| 1,001 | 6.53 | 7.58 (+16.0%) | 110.56 (−12.9%) |

Five rounds rotated the workload order, each trial starting from a fresh copy of the 101,101-trace fixture. Finder latency is the median of the five per-trial transaction medians. For concurrent latency, only complete finder calls inside the writer’s measured interval counted. Throughput is the median of five rates, each measured over 512 committed traces. [Raw transaction intervals and write acknowledgments](results/chunk-ingest.json.gz) · [PostgreSQL 16.16 run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45740632341)

All **6,761 finder calls** in the mixed trials returned the same ordered 100 traces as the idle controls, including ties across the chunk boundary. A separate session verified the committed trace header and root span for all 7,680 measured writes.

An ordering test made two backdated traces eligible in **one commit between the two SELECTs**. The in-flight batch saw only the arrival in the unread chunk. The next finder call saw both. Each batch matched its expected timestamp and trace-ID order. PostgreSQL’s default read-committed isolation gives each SELECT its own snapshot. [Ordering test](test_chunk_ingest.py) · [PostgreSQL snapshot rules](https://www.postgresql.org/docs/16/transaction-iso.html#XACT-READ-COMMITTED)

### Larger incoming spans

Adding **64 KiB per incoming span reduced ingestion throughput by about 25%** at both experiment counts. The writer slowed in every round. Finder latency moved in both directions across rounds. Percentages compare each workload with its own small-span control rerun on the same machine.

| Added per span | Finder, 1,000 (ms) | Writer, 1,000 (traces/s) | Finder, 1,001 (ms) | Writer, 1,001 (traces/s) |
| --- | ---: | ---: | ---: | ---: |
| Small control | 4.22 | 143.31 | 5.50 | 142.23 |
| 16 KiB | 4.03 (−4.6%) | 133.53 (−6.8%) | 5.90 (+7.3%) | 128.82 (−9.4%) |
| 64 KiB | 4.10 (−3.0%) | 107.45 (−25.0%) | 5.52 (+0.5%) | 105.97 (−25.5%) |

Each trial started with the same 101,101 eligible traces. The writer committed 512 single-span traces with timestamps newer than the archival cutoff. One synthetic text attribute increased the serialized span from 399 bytes to 16,812 or 65,964 bytes. We generated the text before timing and measured MLflow 3.18.0’s span construction, serialization and commit. With the writer alone, throughput fell from 167.02 traces/s to 151.17 and 118.60 traces/s, respectively.

Six rounds balanced the workload and payload order. The table reports the median of six trial rates and six per-trial median finder latencies, counting only complete selections inside the write interval. A separate session verified all 27,648 measured writes and their payloads. All 28,949 finder calls in the mixed trials returned the expected 100 traces in order. [Raw timings and stored byte counts](results/payload-study.json.gz) · [PostgreSQL 16.16 run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45743470444)

## Quick start

Inspect the saved measurements with Git and Python 3.10+. The report reader uses only the standard library. This example selects the 100,000-trace workload with a batch limit of 100, 95% already archived, and only the rare experiment (ID 8).

```sh
git clone https://github.com/kenchengkc/mlflow-trace-archive-bench.git
cd mlflow-trace-archive-bench
python3 summarize.py results/strategy-study/matrix.json.gz \
  --scope 8 --batch-size 100 --archived-pct 95 --with-planning
```

For that workload the bounded probe took **21.52 ms**, compared with **17.26 ms** for the global query. At the same archive depth across all eight experiments, the probe used only 26% of the global query’s time. Remove the filters to inspect all 45 cases.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="results/strategy-study/scope-and-depth-dark.png">
  <img src="results/strategy-study/scope-and-depth.png" alt="Bounded probe time divided by global query time on 100,000 traces. At 95% archived, the ratios are 0.40 for the frequent experiment, 1.25 for the rare experiment and 0.26 for all eight. Values below one favor the probe.">
</picture>

Values below 1× favor the bounded probe. [Full timing table](results/strategy-study/matrix.md) · [Raw samples and query plans](results/strategy-study/matrix.json.gz) · [Workload and timing method](results/strategy-study/README.md#workload-and-timing)

## Reproduce the benchmark

On macOS or Linux, put the PostgreSQL server tools (`initdb`, `pg_ctl`, and `psql`) on your `PATH` and run the commands as a non-root user. The runner creates a temporary cluster on a private Unix socket and removes it on exit. To build both matched queues and print the comparison, run the following from the checkout.

```sh
python3 matched_queues.py --output matched-queues.json.gz
```

To inspect the published comparison with Python alone, run `python3 summarize.py results/strategy-study/matched-queues.json.gz --with-planning --buffers`.

To repeat the driver comparison, install the pinned MLflow release and driver in a virtual environment. The study initializes a fresh MLflow database for each queue.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-driver.txt
.venv/bin/python driver_study.py --output driver-study.json.gz
```

With the same environment, run the concurrency checks and compare the three selectors on each queue. Add `--scope 8` to time the rare experiment or `--rows 1000000 --fresh-only` to reproduce the compilation case.

```sh
.venv/bin/python -m unittest -v test_two_step
.venv/bin/python two_step_study.py --output two-step.json.gz
```

To compare 1,000 and 1,001 selected experiments on the same database and verify the merged order, run the chunk study.

```sh
.venv/bin/python chunk_study.py --output chunk-study.json.gz
```

To repeat the same comparison with a concurrent trace writer, run the ingestion study. It also measures the finder alone and the writer alone on the same starting fixture.

```sh
.venv/bin/python chunk_ingest_study.py --output chunk-ingest.json.gz
```

To compare incoming span sizes at a fixed trace count, run the payload study in the same environment.

```sh
.venv/bin/python payload_study.py --output payload-study.json.gz
```

The million-trace study uses the following commands.

```sh
python3 study.py --rows 1000000 --scope 1,2,3,4,5,6,7,8 \
  --archived-pct 0 95 --batch-size 100 --samples 6 --output study-result.json.gz
python3 summarize.py study-result.json.gz --with-planning --buffers
```

For a **SQL-only reproduction**, run the following against an existing scratch database named `scratch` with permission to create a schema. The script seeds 100,000 traces, prints all three query plans, verifies the ordered results, and drops its schema on success. [Standalone SQL](results/strategy-study/late-queue.sql)

```sh
psql -X -d scratch -f results/strategy-study/late-queue.sql
```

## Contributing

For query changes, include a test with the expected trace IDs in order and run `python3 -m unittest -v`. For performance findings, include the PostgreSQL version, command, and result JSON so others can inspect the plan. [Correctness tests](test_strategies.py) · [Project roadmap](ROADMAP.md)

## License

Copyright 2026 Ken Cheng. Distributed under the [Apache License 2.0](LICENSE).
