# Contribution plan

## Completed

- Reproduced the effect of MLflow’s archival index on PostgreSQL 14 and 16. A late batch can require a long ordered scan before enough traces qualify. [Original run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45566898505)
- Compared the shipped global query, the operator’s proposed per-experiment approach, and a bounded probe. Ran 45 workload combinations at 100,000 traces and two at one million. Every comparison checked the complete ordered result. [Study](results/strategy-study/)
- Packaged a single SQL file that a maintainer can run against a scratch PostgreSQL database. It includes the schema, data, three execution plans and a result check. [Reproducer](results/strategy-study/late-queue.sql)

## Next upstream decision

The global query still wins important cases. The smallest contribution to discuss with MLflow maintainers is a PostgreSQL benchmark with **two cases** on the same query shape. Run it once on an untouched queue and once after most traces have been archived. Either case alone can reward a change that makes the other substantially slower. The standalone reproducer and paired plans are ready for that discussion. [Existing MLflow tracing benchmarks](https://github.com/mlflow/mlflow/tree/e2947be527692c6e8fa8fb48d306e2f161fc75a9/dev/benchmarks/tracing) · [Contribution guide](https://github.com/mlflow/mlflow/blob/e2947be527692c6e8fa8fb48d306e2f161fc75a9/CONTRIBUTING.md)

The million-trace control found that planning the expensive fallback can trigger just-in-time compilation even when the prefix fills the batch. A targeted follow-up would issue the fallback only when needed, use a consistent transaction snapshot across both statements, and account for the extra database round trip. [Controlled result](results/strategy-study/#why-the-first-batch-was-slow)

Before proposing a production query change, run the two cases through MLflow’s actual SQLAlchemy and driver path. Vary span size and experiment count, and include concurrent ingest and archival workers. Check candidate order, query latency, and ingestion throughput under writes. A database benchmark can establish where a rewrite helps. These additional tests determine whether it belongs in the tracking server.
