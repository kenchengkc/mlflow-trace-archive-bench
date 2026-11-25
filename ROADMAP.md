# Contribution plan

**Engineering question.** Can MLflow find the next batch of old traces efficiently when many of the oldest traces have already moved to archival storage? A trace represents one request. Each span records one step in that request. This benchmark measures the SQL operation that chooses which traces move next.

| Milestone | Deliverable | Evidence needed |
| --- | --- | --- |
| 1. Establish a baseline | A local PostgreSQL fixture, the query with and without the shipped index, and a correctness test with known answers. | Compare 10k and 100k traces at three levels of archival progress. Record the ordered-result digest, raw plans, individual timings, and index size. |
| 2. Find where the plan changes | Sweep the batch limit (3, 100, 1,000), one vs. many selected experiments, and archive progress from 0% to 99%. Repeat the interesting cases with one million seeded traces and both measurement orders. | Publish the point at which the planner changes strategy, how many trace rows it visits to return each batch, and whether the trend repeats across runs on PostgreSQL 16. Include ties and an exhausted queue. |
| 3. Test one alternative | Prototype selecting the oldest candidates per experiment using the existing `(experiment_id, timestamp_ms)` index, then merging the ordered results. The original reporter suggested this approach. | Compare against the shipped query on identical inputs. Require the same ordered IDs across experiments with tied timestamps and uneven sizes. Measure the extra cost of querying many small experiments and inserting new traces before recommending a change. |
| 4. Make a small upstream contribution | Bring a minimal slow case, paired plans, and exact reproduction steps to MLflow’s tracing maintainers. Discuss whether a performance test or a query patch belongs in their existing benchmark suite. | Link the prior work, publish the SQL and results independently, and confirm scope with maintainers. Submit the agreed change after upstream triage. |

Milestone 1 is implemented. The project’s first decision point is whether the late-queue slowdown repeats when experiment selectivity and table size change. If it does, a maintainer can review a small reproducer before evaluating a change to production code.

The fixture models one experiment chunk from the open-source tracking store. Payloads and tags are synthetic. Full scheduler latency, artifact-transfer performance, concurrent ingestion, and managed Databricks infrastructure are separate measurements.

[Original operator proposal](https://github.com/mlflow/mlflow/issues/24895) · [Existing MLflow tracing benchmarks](https://github.com/mlflow/mlflow/tree/e2947be527692c6e8fa8fb48d306e2f161fc75a9/dev/benchmarks/tracing) · [MLflow contribution guide](https://github.com/mlflow/mlflow/blob/e2947be527692c6e8fa8fb48d306e2f161fc75a9/CONTRIBUTING.md)
