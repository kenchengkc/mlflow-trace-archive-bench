# Concurrent trace ingestion

These studies measure archival selection and trace ingestion on the same PostgreSQL database. Selecting 1,000 experiments uses one candidate query. Selecting 1,001 uses two queries and merges the results. [Project overview](../../README.md) · [Experiment chunk boundary](../finder/README.md#from-1000-to-1001-experiments)

## Selection during trace ingestion

A separate writer process committed one completed, single-span trace per call to MLflow’s `log_spans`, alternating between experiments 1,000 and 1,001. Incoming timestamps were newer than the archival cutoff. The expected oldest 100 traces therefore stayed fixed while the finder and writer used the same database. [Writer](../../chunk_ingest.py) · [MLflow ingestion path](https://github.com/mlflow/mlflow/blob/ba41106d110b67096384da12b6b85473457598db/mlflow/store/tracking/sqlalchemy_store.py#L5802-L5823)

With both processes active, **selection took about 16% longer and ingestion throughput fell about 13%** at both experiment counts. The writer alone committed **126.95 traces/s**. Percentage changes use the finder-only and writer-only controls measured in this run.

| Selected experiments | Finder alone (ms) | Finder with writer (ms) | Writer with finder (traces/s) |
| ---: | ---: | ---: | ---: |
| 1,000 | 4.98 | 5.77 (+15.8%) | 110.20 (−13.2%) |
| 1,001 | 6.53 | 7.58 (+16.0%) | 110.56 (−12.9%) |

Five rounds rotated the workload order, each trial starting from a fresh copy of the 101,101-trace fixture. Finder latency is the median of the five per-trial transaction medians. For concurrent latency, only complete finder calls inside the writer’s measured interval counted. Throughput is the median of five rates, each measured over 512 committed traces. [Raw transaction intervals and write acknowledgments](../chunk-ingest.json.gz) · [PostgreSQL 16.16 run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45740632341)

All **6,761 finder calls** in the mixed trials returned the same ordered 100 traces as the idle controls, including ties across the chunk boundary. A separate session verified the committed trace header and root span for all 7,680 measured writes.

An ordering test made two backdated traces eligible in **one commit between the two SELECTs**. The in-flight batch saw only the arrival in the unread chunk. The next finder call saw both. Each batch matched its expected timestamp and trace-ID order. PostgreSQL’s default read-committed isolation gives each SELECT its own snapshot. [Ordering test](../../test_chunk_ingest.py) · [PostgreSQL snapshot rules](https://www.postgresql.org/docs/16/transaction-iso.html#XACT-READ-COMMITTED)

## Larger incoming spans

Adding **64 KiB per incoming span reduced ingestion throughput by about 25%** at both experiment counts. The writer slowed in every round. Finder latency moved in both directions across rounds. Percentages compare each workload with its own small-span control rerun on the same machine.

| Added per span | Finder, 1,000 (ms) | Writer, 1,000 (traces/s) | Finder, 1,001 (ms) | Writer, 1,001 (traces/s) |
| --- | ---: | ---: | ---: | ---: |
| Small control | 4.22 | 143.31 | 5.50 | 142.23 |
| 16 KiB | 4.03 (−4.6%) | 133.53 (−6.8%) | 5.90 (+7.3%) | 128.82 (−9.4%) |
| 64 KiB | 4.10 (−3.0%) | 107.45 (−25.0%) | 5.52 (+0.5%) | 105.97 (−25.5%) |

Each trial started with the same 101,101 eligible traces. The writer committed 512 single-span traces with timestamps newer than the archival cutoff. One synthetic text attribute increased the serialized span from 399 bytes to 16,812 or 65,964 bytes. We generated the text before timing and measured MLflow 3.18.0’s span construction, serialization and commit. With the writer alone, throughput fell from 167.02 traces/s to 151.17 and 118.60 traces/s, respectively.

Six rounds balanced the workload and payload order. The table reports the median of six trial rates and six per-trial median finder latencies, counting only complete selections inside the write interval. A separate session verified all 27,648 measured writes and their payloads. All 28,949 finder calls in the mixed trials returned the expected 100 traces in order. [Raw timings and stored byte counts](../payload-study.json.gz) · [PostgreSQL 16.16 run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45743470444)

## Where the 64 KiB write time goes

**Writing the span row was the largest repeatable cost.** With 1,000 and 1,001 selected experiments, that one SQL statement took an extra **1.509 and 1.542 ms per trace**, respectively. Encoding the span dictionary as JSON added **0.585 and 0.595 ms**. Both steps took longer in all eight rounds at each experiment count. The other five SQL writes together added about 0.02 ms per trace.

All times below are arithmetic means in **milliseconds per committed trace**. Each trial ingested the same 512 single-span traces while MLflow’s archival finder ran on a separate connection. The small and larger spans contained 399 and 65,964 bytes of serialized JSON, respectively.

| Mean writer time per trace | 1,000 experiments, small → +64 KiB | Change | 1,001 experiments, small → +64 KiB | Change |
| --- | ---: | ---: | ---: | ---: |
| Build the input span | 0.059 → 0.162 | +0.103 | 0.059 → 0.164 | +0.105 |
| Serialize the span for storage | 0.107 → 0.698 | +0.591 | 0.106 → 0.709 | +0.602 |
| Execute SQL writes | 1.008 → 2.539 | +1.531 | 1.017 → 2.578 | +1.561 |
| Commit | 2.016 → 1.708 | −0.308 | 1.673 → 2.267 | +0.593 |
| Other SQL and application time | 5.822 → 5.848 | +0.025 | 5.679 → 5.956 | +0.277 |
| Total | 9.013 → 10.954 | +1.941 | 8.534 → 11.673 | +3.140 |
| Total without phase timers | 8.036 → 11.129 | +3.093 | 8.080 → 10.669 | +2.589 |

Span construction includes encoding the incoming attribute. Storage serialization covers MLflow’s conversion to a dictionary and its JSON encoder. SQL writes measure time in the database driver, including parameter handling and PostgreSQL execution. Commit covers both driver calls per trace, one for the experiment lookup and one for the write transaction. [MLflow storage path](https://github.com/mlflow/mlflow/blob/ba41106d110b67096384da12b6b85473457598db/mlflow/store/tracking/sqlalchemy_store.py#L5822-L6150) · [Timer boundaries](../../ingest_phases.py)

Commit time rose in some rounds and fell in others at both experiment counts, with synchronous commit enabled. Without phase timers, larger spans reduced throughput by **27.8% and 24.3%**, respectively. The estimates of added end-to-end time with and without timers differed by 1.15 and 0.55 ms per trace. All 32,768 measured writes passed readback checks, and all 63,248 finder calls in the mixed trials returned the expected ordered batch. [Per-trace samples](../phase-study.json.gz) · [PostgreSQL 16.16 run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45745858658)

## Inside the span upsert

**PostgreSQL execution accounted for 76% of the extra driver time** when each incoming span carried 64 KiB more text. At 1,000 selected experiments, the driver took an extra 1.886 ms per upsert and PostgreSQL execution took an extra 1.441 ms. At 1,001 experiments, the increases were 1.850 ms and 1.412 ms. The difference includes parameter handling, SQL parsing and planning, scheduling, and the local round trip.

Each row is a mean per upsert, with differences calculated before rounding. The driver timer and PostgreSQL’s `pg_stat_statements` counters cover the **same SQL calls**. Every upsert inserted a new span. The executor timer groups row, index, TOAST and WAL work together.

| Experiments | Incoming span | Driver (ms) | PostgreSQL execution (ms) | Difference (ms) | WAL (bytes) | TOAST chunks | TOAST data (bytes) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | Small | 0.315 | 0.101 | 0.215 | 1,035 | 0 | 0 |
| 1,000 | +64 KiB | 2.201 | 1.541 | 0.660 | 71,125 | 34 | 65,964 |
| 1,001 | Small | 0.316 | 0.101 | 0.215 | 1,035 | 0 | 0 |
| 1,001 | +64 KiB | 2.166 | 1.513 | 0.654 | 71,125 | 34 | 65,964 |

The 399-byte small span fit in the main row. PostgreSQL stored each larger span’s 65,964 bytes of JSON out of line in **34 TOAST chunks**. Per large span, the TOAST table grew by about 68 KiB and its index by 752 bytes. The upsert generated **68.7× as many write-ahead log (WAL) bytes**. These statement counters exclude the later transaction commit. [PostgreSQL statement counters](https://www.postgresql.org/docs/16/pgstatstatements.html) · [TOAST storage](https://www.postgresql.org/docs/16/storage-toast.html)

Eight rounds kept the trace count fixed at 512 per trial while MLflow’s finder ran concurrently. Counter and storage snapshots excluded the warmup. We matched all 16,384 driver upserts to server executions and verified the expected ordered batch on all 25,442 finder calls. [Raw driver times, server counters and TOAST snapshots](../upsert-study.json.gz) · [PostgreSQL 16.16 run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45757575190)

## Reproduce the studies

Run from the repository root on macOS or Linux as a non-root user, with PostgreSQL’s `initdb`, `pg_ctl`, and `psql` on your `PATH`. The upsert study also needs the `pg_stat_statements` extension installed for that PostgreSQL version. Each runner creates and removes its own temporary database cluster.

Install the pinned MLflow and driver dependencies once, then run the study you want.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-driver.txt

# Compare the finder alone, writer alone, and both together.
.venv/bin/python chunk_ingest_study.py --output chunk-ingest.json.gz

# Compare incoming span sizes.
.venv/bin/python payload_study.py --output payload-study.json.gz

# Time serialization, database writes, and commits.
.venv/bin/python phase_study.py --output phase-study.json.gz

# Measure PostgreSQL execution, WAL and TOAST storage for the span upsert.
.venv/bin/python upsert_study.py --output upsert-study.json.gz
```
