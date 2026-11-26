# Choosing the next traces to archive

[Project overview](../../README.md)

MLflow’s timestamp and trace-ID index lets PostgreSQL walk traces in archival order. That is cheap when the oldest rows qualify. Once the oldest rows have moved out of the tracking store, the same walk can spend most of its time checking rows that will never be returned.

## Three queries, identical answers

| Query | How it chooses the next batch |
| --- | --- |
| [Global](../../sql/candidates.sql) | Applies MLflow’s eligibility rules across the selected experiments and returns the oldest K traces, breaking timestamp ties by ID. |
| [Per experiment](../../sql/per_experiment.sql) | Takes the oldest K eligible traces per experiment, then merges and limits the combined rows. A trace ranked below K in its own experiment cannot enter the global first K. This implements the operator’s suggested approach in one SQL statement. |
| [Bounded probe](../../sql/bounded_probe.sql) | Examines the oldest matching trace headers, up to twice K with a cap of 4,096. It checks their spans and tags. If that fills the batch or exhausts the headers, it returns the prefix’s eligible rows. Otherwise it recomputes eligibility across the selected experiments and sorts those results. |

### How the bounded probe chooses a batch

```mermaid
flowchart TD
    probe["Read the oldest matching headers up to the probe limit"] --> check["Check spans and tags for eligibility"]
    check --> enough{"Full batch or fewer headers than the probe limit?"}
    enough -->|Yes| prefix["Return the eligible prefix rows in archival order"]
    enough -->|No| fallback["Filter all matching traces and return the oldest eligible batch"]
```

For a batch of 100, the probe limit is 200 headers. If only 150 matching headers exist, the probe has seen them all and can return a partial batch. If it sees 200 and finds only 99 eligible traces, it takes the fallback. Both branches see the same database snapshot. The cap applies to headers passed to eligibility checks. PostgreSQL may read more index entries or pages to find them.

## What changed at one million traces

On one million synthetic traces, all three queries selected the oldest 100 eligible traces across eight experiments. Times below are the median and observed range of six PostgreSQL 16.16 planning and execution measurements. Both queues used the same indexes.

| Query | Fresh queue (ms) | Oldest 95% archived (ms) | Shared-buffer accesses, 95% archived |
| --- | ---: | ---: | ---: |
| Global (MLflow) | **1.24** (1.20–1.33) | 1,315.50 (1,311.40–1,331.15) | 4,578,536 |
| Per-experiment merge | 4.51 (4.38–4.69) | 1,848.73 (1,804.38–1,865.08) | 4,651,686 |
| Bounded probe | 27.95 (27.09–30.26) | **284.59** (282.93–287.06) | 61,117 |

The global query’s ordered scan returned 853,678 trace headers into subsequent checks. The bounded query examined 200 headers, found no qualifying rows, and ran its fallback. The fallback produced 36,191 eligible rows for the final top-100 sort. Buffer counts are root-level hits plus reads from one representative plan per query and include repeated visits to a page. [Late-queue raw plans](million-late.json.gz)

On the fresh queue, the bounded query filled the batch from its prefix. Its full eligibility branch executed zero times. PostgreSQL nevertheless reported 68 just-in-time (JIT) compilation functions in that query’s plan. PostgreSQL decides whether to compile using the estimated cost of the whole plan. [Early-queue raw plans](million-start.json.gz) · [PostgreSQL JIT decisions](https://www.postgresql.org/docs/16/jit-decision.html)

## Why the first batch was slow

A follow-up run alternated the same bounded SQL with JIT enabled and disabled on the same fixture. Each setting ran six times, with the first setting alternating every round. The database returned the same ordered rows and the same estimated plan cost in each mode.

| Oldest traces archived | JIT on | JIT off | Full fallback executions per query |
| ---: | ---: | ---: | ---: |
| 0% | 26.04 ms | 1.65 ms | 0 |
| 95% | 288.00 ms | 254.47 ms | 1 |

At 0%, the planner estimated a cost of 301,090 against a JIT threshold of 100,000. The query compiled 68 functions with JIT enabled despite taking the short path. Switching off compilation removed about 24 ms of the first-batch latency on this fixture. The experiment changes a session setting for the measured query. A production choice would also need to account for other queries sharing a connection. [Paired raw timings and plans](million-jit.json.gz) · [Control run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45576509086)

## Two queues with 95% archived

**Changing which traces were archived flipped the fastest query.** Both fixtures started with identical headers for 100,000 traces. Of 72,386 initially eligible traces in the eight selected experiments, each fixture archived 68,766 and left 3,620. One archived the oldest traces first. The other used a fixed seed to scatter archival through time, matching the archived counts for each experiment and span count.

| Archive layout | Position of 100th eligible trace | Global (ms) | Per experiment (ms) | Bounded probe (ms) |
| --- | ---: | ---: | ---: | ---: |
| Oldest first | 85,489 | 142.10 | 154.00 | **36.27** |
| Scattered | 2,422 | **5.26** | 42.07 | 37.23 |

Both global queries used the same timestamp and trace-ID index. The oldest-first scan passed **35× as many headers** matching the experiments, cutoff and status to the eligibility checks. The global plan recorded 369,267 shared-buffer accesses on that queue and 11,062 on the scattered queue. The bounded probe found zero and seven eligible traces, respectively, in its first 200 headers, so it ran the full fallback in both layouts.

Times are medians of six warm PostgreSQL 16.16 runs per query and include database planning and execution. Buffer counts come from one representative plan per layout and include repeated accesses. All three queries returned the independently verified ordered batch for each fixture. [Raw samples and plans](matched-queues.json.gz) · [PostgreSQL run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45667039308)

## How experiment scope changes the result

The 100,000-trace fixture also varies which experiments the query selects. With 95% archived and a batch limit of 100, the bounded probe took **21.52 ms** for rare experiment 8 alone, compared with **17.26 ms** for the global query. Across all eight experiments at the same depth, the probe used 26% of the global query’s time.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="scope-and-depth-dark.png">
  <img src="scope-and-depth.png" alt="Bounded probe time divided by global query time on 100,000 traces. At 95% archived, the ratios are 0.40 for the frequent experiment, 1.25 for the rare experiment and 0.26 for all eight. Values below one favor the probe.">
</picture>

Each marker shows bounded-probe time divided by global-query time. Values below 1× favor the bounded probe. [Full timing table](matrix.md) · [Raw samples and query plans](matrix.json.gz)

## Workload and timing

- The 100,000-trace matrix crosses three scopes (frequent experiment 1, rare experiment 8, all eight), batch limits of 3, 100 and 1,000, and 0%, 50%, 95%, 99% and 100% archival. Archival percentage refers to the oldest initially eligible traces across the eight-experiment population. Each report includes the actual remaining count for the selected scope. [Full table](matrix.md) · [All timings and plans](matrix.json.gz)
- For each size and archive percentage, the runner starts a fresh PostgreSQL cluster, seeds deterministic data, simulates archival, refreshes statistics, and installs the shipped archival index. All three variants run on that same fixture. Correctness tests independently cover missing and empty spans, failure tags, rare and absent experiments, tied timestamps, arrivals between batches, and batch sizes above the prefix cap. [Fixture](../../sql/seed.sql) · [Tests](../../test_strategies.py)
- Each query gets an instrumented warm-up. The six measured rounds cover every ordering of the three strategies. Each timing uses a new client session. The headline metric adds PostgreSQL’s reported planning and execution time **for each sample** before taking the median. Connection setup, network transfer and artifact uploads are outside this interval. Each saved plan is the sample nearest the median **execution** time. [Measurement code](../../study.py) · [PostgreSQL 16 run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45575683264)
- These comparisons cover complete SQL rewrites. They change how the planner sees experiment and tag filters as well as how it searches the queue. Absolute times depend on the machine, planner statistics, data distribution and cache state. The fixture uses short synthetic spans, one experiment chunk, literal parameters and an idle database. The reported time covers candidate selection in the open-source tracking store.

## Replay

Run the commands from the repository root. Python 3.10+ can read the saved measurements directly. Remove the scope, batch-size and archive-depth filters to see all 45 cases.

```sh
python3 summarize.py results/strategy-study/matrix.json.gz \
  --scope 8 --batch-size 100 --archived-pct 95 --with-planning
python3 summarize.py results/strategy-study/matched-queues.json.gz --with-planning --buffers
```

To run fresh measurements on macOS or Linux, put `initdb`, `pg_ctl` and `psql` on your `PATH` and use a non-root account. The Python runners start a temporary PostgreSQL cluster on a private Unix socket and remove it on exit.

```sh
# Compare the two 95%-archived layouts.
python3 matched_queues.py --output matched-queues.json.gz

# Compare the fresh and oldest-first queues at one million traces.
python3 study.py --rows 1000000 --scope 1,2,3,4,5,6,7,8 \
  --archived-pct 0 95 --batch-size 100 --samples 6 --output study-result.json.gz
python3 summarize.py study-result.json.gz --with-planning --buffers
```

Add `--jit-control` to the `study.py` command to repeat the compilation experiment.

For a SQL-only reproduction, run `psql -X -d scratch -f results/strategy-study/late-queue.sql` against a scratch database where you can create a schema. The script seeds 100,000 traces, prints all three query plans, verifies the ordered rows, and drops its schema on success. A failed run may leave the printed schema in the database. [Standalone SQL](late-queue.sql) · [SQL exporter](../../reproduce.py)

To regenerate the README charts, install Matplotlib in your plotting environment and run `python3 results/strategy-study/plot.py` from the repository root.
