# Contribution roadmap

MLflow added an index so PostgreSQL can read traces in archival order. On our million-trace fixture, the indexed query selected 100 traces in 1.24 ms on a fresh queue and 1,315.50 ms after the oldest 95% of eligible traces were archived. Those are median database planning and execution times. The candidate finder still has the same implementation at the November 25, 2026 upstream revision. [Index change](https://github.com/mlflow/mlflow/pull/24976) · [Measurements](results/strategy-study/) · [Pinned MLflow finder](https://github.com/mlflow/mlflow/blob/dc89dad0dd603499fd71304c838e62ffbc14dfb9/mlflow/store/tracking/sqlalchemy_store.py#L7271-L7350)

Completed work includes index comparisons on PostgreSQL 14 and 16, a 45-case study at 100,000 traces, two million-trace cases and a standalone SQL reproducer. [Index comparison](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45566898505) · [Study matrix](results/strategy-study/matrix.md) · [SQL reproducer](results/strategy-study/late-queue.sql)

## 1. Same archive percentage, different queue

**Completed.** The 100,000-trace comparison matched archived counts by experiment and span count. Both layouts left 3,620 eligible traces. Scattering the archived traces moved the 100th eligible trace from header 85,489 to header 2,422 and made the global query the fastest of the three on PostgreSQL 16.16. Run `python3 matched_queues.py` to rebuild both queues and check the ordered results. [Matched-queue findings](README.md#two-queues-with-95-archived) · [Saved plans](results/strategy-study/matched-queues.json.gz)

## 2. Reproduce the result through MLflow

**Completed.** The MLflow 3.18.0 finder reproduced all three ordered batches on migrated PostgreSQL databases. The oldest-first queue still took the longest. A different tag-index order changed the fresh-queue plan. The driver runner now provides a baseline for testing the two-step query below. [Driver results](README.md#through-mlflows-database-driver) · [Benchmark code](driver_study.py)

## 3. Run the expensive query only when needed

**Completed.** Sending the fallback separately reduced the fresh million-trace median from 14.79 ms to 1.40 ms. Both reads preserved their original snapshot across a concurrent commit. The oldest-first queue benefited from the full eligibility scan, while MLflow’s finder was 5.1× faster than the two-step selector on the scattered queue at the same archive percentage. The evidence supports contributing the contrasting benchmark cases and keeping the selector experimental. [Driver measurements](README.md#deferring-the-fallback) · [Concurrency test](test_two_step.py)

## 4. Cross the experiment chunk boundary

**Completed.** With 1,000 selected experiments, one query supplied the entire 100-trace batch. Adding experiment 1,001 made MLflow fetch 100 candidates from each of two queries. On a fixture with tied timestamps across the boundary, 50 rows from each query survived the merge. Swapping the tied experiments between chunks preserved the exact order. [Measurements](README.md#from-1000-to-1001-experiments) · [Boundary test](test_chunk_study.py)

## What earns the next experiment

Start with an upstream report or an unexplained result, change one workload property, and publish the smallest case another contributor can replay. The original operator reported thousands of experiments and tens of gigabytes of span data. Concurrent ingestion and larger spans are separate follow-ups if measurements point there. [Operator report](https://github.com/mlflow/mlflow/issues/24895)
