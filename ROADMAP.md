# Contribution roadmap

MLflow added an index so PostgreSQL can read traces in archival order. On our million-trace fixture, the indexed query selected 100 traces in 1.24 ms on a fresh queue and 1,315.50 ms after the oldest 95% of eligible traces were archived. Those are median database planning and execution times. The candidate finder still has the same implementation at the November 25, 2026 upstream revision. [Index change](https://github.com/mlflow/mlflow/pull/24976) · [Measurements](results/strategy-study/) · [Pinned MLflow finder](https://github.com/mlflow/mlflow/blob/dc89dad0dd603499fd71304c838e62ffbc14dfb9/mlflow/store/tracking/sqlalchemy_store.py#L7271-L7350)

Completed work includes index comparisons on PostgreSQL 14 and 16, a 45-case study at 100,000 traces, two million-trace cases and a standalone SQL reproducer. [Index comparison](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45566898505) · [Study matrix](results/strategy-study/matrix.md) · [SQL reproducer](results/strategy-study/late-queue.sql)

## 1. Same archive percentage, different queue

**Completed.** The 100,000-trace comparison matched archived counts by experiment and span count. Both layouts left 3,620 eligible traces. Scattering the archived traces moved the 100th eligible trace from header 85,489 to header 2,422 and made the global query the fastest of the three on PostgreSQL 16.16. Run `python3 matched_queues.py` to rebuild both queues and check the ordered results. [Matched-queue findings](README.md#two-queues-with-95-archived) · [Saved plans](results/strategy-study/matched-queues.json.gz)

## 2. Reproduce the result through MLflow

**Completed.** The MLflow 3.18.0 finder reproduced all three ordered batches on migrated PostgreSQL databases. The oldest-first queue still took the longest. A different tag-index order changed the fresh-queue plan. The driver runner now provides a baseline for testing the two-step query below. [Driver results](README.md#through-mlflows-database-driver) · [Benchmark code](driver_study.py)

## 3. Run the expensive query only when needed

The existing bounded probe checks the oldest 200 matching headers for a batch of 100. On the fresh million-trace fixture it filled the batch there, yet PostgreSQL still compiled code for a plan containing the unused fallback. Test a version that sends the full eligibility query only when the prefix cannot answer. On fallback, recompute the whole batch. [Compilation experiment](results/strategy-study/#why-the-first-batch-was-slow)

Compare the shipped query, the current probe and the two-step version on the same connection settings. Include the rare experiment where the current probe lost. Count SQL calls and time the full selection transaction, including round trips. For the two-step test, use a short read-only repeatable-read transaction so both reads see the same data. Between reads, have a second connection commit a change that makes an older trace eligible. Check the answer against the original snapshot and verify the next transaction sees the change. [Rare-experiment result](results/strategy-study/matrix.md) · [PostgreSQL snapshot rules](https://www.postgresql.org/docs/16/transaction-iso.html#XACT-REPEATABLE-READ)

Advance a query change only if it preserves the ordered results, removes a measurable fresh-queue cost, and retains a late-queue benefit through the driver. Publish any losses alongside the wins. The paired benchmark stands on its own if the rewrite falls short.

## What earns the next experiment

Start with an upstream report or an unexplained result, change one workload property, and publish the smallest case another contributor can replay. The next candidate is experiment count. The original operator had thousands of experiments, while this study uses eight. Compare 1,000 and 1,001 selected experiments. Put eligible traces with tied timestamps in both query chunks, then measure query count and verify the merged order. Concurrent ingestion and larger spans are separate follow-ups if measurements point there. [Operator report](https://github.com/mlflow/mlflow/issues/24895) · [MLflow’s chunk size](https://github.com/mlflow/mlflow/blob/dc89dad0dd603499fd71304c838e62ffbc14dfb9/mlflow/store/tracking/utils/trace_archival.py#L24)
