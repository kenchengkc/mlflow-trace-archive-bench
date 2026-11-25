# MLflow trace archival benchmark

MLflow periodically asks its tracking database for the oldest completed traces ready to archive. An operator reported that PostgreSQL took minutes to choose just three. MLflow shipped an index on timestamp and trace ID. This project tests what happens when the oldest traces have already been archived and the database must look farther to fill a batch. [Operator report](https://github.com/mlflow/mlflow/issues/24895) · [Shipped fix](https://github.com/mlflow/mlflow/pull/24976)

## What we found

On **one million synthetic traces**, a query that checks a short prefix of the queue and falls back to filtering the full set was **4.62× faster at 95% archived**. The shipped query was **22.5× faster at 0% archived**. Each query returned the same 100 traces in the same order.

| Oldest eligible traces already archived | Shipped global query | Per-experiment merge | Bounded probe |
| ---: | ---: | ---: | ---: |
| 0% | **1.24 ms** | 4.51 ms | 27.95 ms |
| 95% | 1,315.50 ms | 1,848.73 ms | **284.59 ms** |

Each number is the median of six paired planning-plus-execution timings on PostgreSQL 16.16. All three queries used the shipped indexes. The 47-case study covers different batch sizes, frequent and rare experiments, timestamp ties in the data, and exhausted queues. [Results, plans, and limitations](results/strategy-study/) · [All 45 cases at 100,000 traces](results/strategy-study/matrix.md)

![Time for the bounded probe divided by time for the global query. At 95% archived the bounded probe was faster across all experiments and slower for the rare experiment.](results/strategy-study/scope-and-depth.png)

A follow-up isolated why the first batch was slow. PostgreSQL compiled part of the query even though the fallback never ran. Alternating just-in-time compilation on and off reduced that batch from **26.04 ms to 1.65 ms** with compilation off. [Controlled comparison](results/strategy-study/#why-the-first-batch-was-slow)

## Reproduce it

**With PostgreSQL tools installed**, run these commands from the checkout as a regular OS user. Python 3.10+ uses only the standard library. The runner creates and removes its own database on a private socket. Put `initdb`, `pg_ctl`, and `psql` on your path.

```sh
python3 -m unittest -v
python3 study.py --output study-result.json.gz
python3 summarize.py study-result.json.gz --with-planning --buffers
```

**With an existing scratch PostgreSQL database**, the checked-in SQL file creates a 100,000-trace fixture, prints three query plans, verifies the ordered results, and removes its schema on success. This route needs only `psql` and permission to create a schema. [Download the standalone SQL](https://raw.githubusercontent.com/kenchengkc/mlflow-trace-archive-bench/main/results/strategy-study/late-queue.sql)

```sh
psql -X -d scratch -f results/strategy-study/late-queue.sql
```

To export a different workload, run `python3 reproduce.py --help`. To repeat the original comparison with and without MLflow’s archival index, run `python3 benchmark.py`. [Original PostgreSQL 16 measurements](results/postgresql-16-linux.json)

## Connection to Databricks

Databricks uses MLflow Tracing to record individual model and tool calls for debugging, evaluation, and monitoring. This repository measures candidate selection in the open-source MLflow PostgreSQL tracking store. Its contribution is a repeatable database workload, a tested alternative, and concrete cases where that alternative regresses. Databricks also supports storing traces in Unity Catalog. [Databricks tracing overview](https://docs.databricks.com/aws/en/mlflow3/genai/tracing/overview) · [Upstream candidate query](https://github.com/mlflow/mlflow/blob/3128eb46928fab08500124e9e0cde6791cf845aa/mlflow/store/tracking/sqlalchemy_store.py) · [Contribution plan](ROADMAP.md)

Copyright 2026 Ken Cheng. Independent project, licensed under Apache License 2.0. [License](LICENSE)
