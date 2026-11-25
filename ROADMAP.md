# Contribution roadmap

MLflow added an index so PostgreSQL can read traces in archival order. On our million-trace fixture, the indexed query selected 100 traces in 1.24 ms on a fresh queue and 1,315.50 ms after the oldest 95% of eligible traces were archived. Those are median database planning and execution times. The candidate finder still has the same implementation at the November 25, 2026 upstream revision. [Index change](https://github.com/mlflow/mlflow/pull/24976) · [Measurements](results/strategy-study/) · [Pinned MLflow finder](https://github.com/mlflow/mlflow/blob/dc89dad0dd603499fd71304c838e62ffbc14dfb9/mlflow/store/tracking/sqlalchemy_store.py#L7271-L7350)

Completed work includes index comparisons on PostgreSQL 14 and 16, a 45-case study at 100,000 traces, two million-trace cases and a standalone SQL reproducer. [Index comparison](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45566898505) · [Study matrix](results/strategy-study/matrix.md) · [SQL reproducer](results/strategy-study/late-queue.sql)

## 1. Same archive percentage, different queue

**Question.** How much does the position of the remaining traces change the cost of selecting a batch?

Start with 100,000 traces, eight experiments and a batch of 100. In one fixture, archive the oldest 95% of initially eligible traces, as today. In a second, spread the same number of archived traces through time with a fixed seed. Use identical trace headers and match archived counts by experiment and span count. Check every strategy against independently calculated ordered results for its own fixture.

Publish one command that produces both fixtures and a table showing the position of the 100th eligible trace among matching headers, the scan PostgreSQL chose, its buffer accesses and median latency. If the query stops using an ordered scan, preserve that plan too. This pair lets a maintainer distinguish a long run of already archived traces from a high archive percentage alone. [Existing fixture](sql/seed.sql) · [Current comparison](results/strategy-study/matrix.md)

## 2. Reproduce the result through MLflow

Run the fresh queue and both 95% layouts against a PostgreSQL database initialized with MLflow’s migrations. Invoke the pinned SQLAlchemy store’s candidate finder with its normal driver and bound parameters. Check the ordered trace IDs, experiment IDs and timestamps against the fixture. Record the time the caller waits on a warm connection and collect database plans separately.

The deliverable is a benchmark a maintainer can run against an MLflow checkout. If the store produces a different plan or reverses a timing result from raw SQL, publish that difference before testing a rewrite. The fresh and oldest-first cases are the first pair to propose for MLflow’s tracing benchmark suite. [Candidate finder](https://github.com/mlflow/mlflow/blob/dc89dad0dd603499fd71304c838e62ffbc14dfb9/mlflow/store/tracking/sqlalchemy_store.py#L7271-L7350) · [MLflow benchmarks](https://github.com/mlflow/mlflow/tree/dc89dad0dd603499fd71304c838e62ffbc14dfb9/dev/benchmarks/tracing)

## 3. Run the expensive query only when needed

The existing bounded probe checks the oldest 200 matching headers for a batch of 100. On the fresh million-trace fixture it filled the batch there, yet PostgreSQL still compiled code for a plan containing the unused fallback. Test a version that sends the full eligibility query only when the prefix cannot answer. On fallback, recompute the whole batch. [Compilation experiment](results/strategy-study/#why-the-first-batch-was-slow)

Compare the shipped query, the current probe and the two-step version on the same connection settings. Include the rare experiment where the current probe lost. Count SQL calls and time the full selection transaction, including round trips. For the two-step test, use a short read-only repeatable-read transaction so both reads see the same data. Between reads, have a second connection commit a change that makes an older trace eligible. Check the answer against the original snapshot and verify the next transaction sees the change. [Rare-experiment result](results/strategy-study/matrix.md) · [PostgreSQL snapshot rules](https://www.postgresql.org/docs/16/transaction-iso.html#XACT-REPEATABLE-READ)

Advance a query change only if it preserves the ordered results, removes a measurable fresh-queue cost, and retains a late-queue benefit through the driver. Publish any losses alongside the wins. The paired benchmark stands on its own if the rewrite falls short.

## What earns the next experiment

Start with an upstream report or an unexplained result, change one workload property, and publish the smallest case another contributor can replay. The next candidate is experiment count. The original operator had thousands of experiments, while this study uses eight. Compare 1,000 and 1,001 selected experiments. Put eligible traces with tied timestamps in both query chunks, then measure query count and verify the merged order. Concurrent ingestion and larger spans are separate follow-ups if measurements point there. [Operator report](https://github.com/mlflow/mlflow/issues/24895) · [MLflow’s chunk size](https://github.com/mlflow/mlflow/blob/dc89dad0dd603499fd71304c838e62ffbc14dfb9/mlflow/store/tracking/utils/trace_archival.py#L24)
