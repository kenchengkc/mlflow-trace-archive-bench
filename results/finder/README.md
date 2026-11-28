# MLflow’s archival candidate finder

These studies time MLflow’s selection of the oldest eligible traces through its SQLAlchemy tracking store. They cover queue layout, a two-step alternative, ordering when MLflow splits a request across two SQL queries, and the cost of draining a queue three traces at a time. [Project overview](../../README.md)

## Through MLflow’s database driver

MLflow 3.18.0 took **112.18 ms** to select 100 traces from the oldest-first queue and **4.84 ms** from the equally archived scattered queue. We ran its actual SQLAlchemy candidate finder on three 100,000-trace databases initialized by MLflow’s migrations. Every timed call returned the expected trace IDs, experiment IDs and timestamps in order, with the same result digest as the earlier raw SQL study. [Upstream finder](https://github.com/mlflow/mlflow/blob/ba41106d110b67096384da12b6b85473457598db/mlflow/store/tracking/sqlalchemy_store.py#L7151-L7230)

| Queue | MLflow finder (ms) | Raw global SQL (ms) | Replay of MLflow’s SQL (ms) |
| --- | ---: | ---: | ---: |
| Fresh | 1.70 | 0.94 | 0.95 |
| 95% archived, oldest first | 112.18 | 111.20 | 110.70 |
| 95% archived, scattered | 4.84 | 4.05 | 4.04 |

The raw column runs this repository’s global query directly. The replay runs the exact SQL and bound parameters captured from MLflow. Each value is the median of 12 caller-side measurements on PostgreSQL 16.16, from opening the managed session through fetching the batch, committing and closing the session. The methods shared one warm psycopg2 connection per queue and ran in all six possible orders. The finder’s median was 0.76 ms above raw SQL on the fresh queue and 0.98 ms above it on the oldest-first queue. [Timing samples, SQL and plans](../driver-study.json.gz) · [Benchmark run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45670762246)

The earlier reduced-schema study measured server planning and execution alone. Its fresh-queue plan scanned 30,882 tag rows looking for a span-location tag. MLflow’s migration puts the tag key first in the primary index, while the reduced fixture puts the trace ID first. Both migrated plans used that index for the lookup and found zero matching tags. [Earlier plan](../strategy-study/matrix.json.gz) · [MLflow migration](https://github.com/mlflow/mlflow/blob/ba41106d110b67096384da12b6b85473457598db/mlflow/store/db_migrations/versions/867495a8f9d4_add_trace_tables.py#L57) · [Reduced schema](../../sql/schema.sql)

## Deferring the fallback

For a batch of 100, the two-step selector checks up to 200 of the oldest matching headers. If it fills the batch or finds fewer than 200 headers, it returns the eligible traces in order. Otherwise, it sends a second query that recomputes the entire batch. Both reads share a short, read-only, repeatable-read transaction. [Selector](../../two_step.py) · [Prefix query](../../sql/two_step_prefix.sql) · [Fallback query](../../sql/two_step_fallback.sql)

| Queue, 100,000 traces | MLflow finder (ms) | Single-statement probe (ms) | Two-step (ms) | Two-step fallback calls |
| --- | ---: | ---: | ---: | ---: |
| Fresh | 1.84 | 1.49 | **1.15** | 0/12 |
| 95% archived, oldest first | 112.71 | 28.46 | 28.96 | 12/12 |
| 95% archived, scattered | **5.36** | 27.72 | 27.41 | 12/12 |

Times are medians of 12 complete selection transactions per method, including checkout, isolation setup and commit on the same warm connection per queue. Every call returned the independently verified ordered batch. The two probes’ timing ranges overlapped within each archived layout. With archival scattered, two-step selection took **5.1× as long as MLflow’s finder**. For the rare experiment alone, all three medians were within 0.5 ms of one another on both archived queues. [Samples and plans](../two-step.json.gz) · [Rare experiment](../two-step-rare.json.gz) · [PostgreSQL 16.16 run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45672585264)

The largest fresh-queue difference appeared at **one million traces**. The single-statement probe took **14.79 ms** and its plan showed 67 just-in-time compilation functions, even though the fallback executed zero times. The two-step selector sent only the prefix query, whose plan showed no compilation, and took **1.40 ms**. MLflow’s finder took **2.10 ms**. [Million-trace samples and plans](../two-step-million-fresh.json.gz)

To verify the order under concurrent writes, a test commits two changes on a second PostgreSQL connection between the probe and fallback. An older trace becomes eligible and a tied candidate is archived. The fallback returns the exact pre-commit batch, including both tied IDs in order. A new transaction sees both changes and agrees with MLflow’s finder. [Concurrency test](../../test_two_step.py)

## From 1,000 to 1,001 experiments

Adding experiment 1,001 to the selection triggered a second candidate query and increased median batch-selection time by **0.90 ms** on this fixture. We asked MLflow 3.18.0 for the oldest 100 traces on the same migrated PostgreSQL 16.16 database, with 101 eligible traces in each of 1,001 experiments. [MLflow’s finder and merge](https://github.com/mlflow/mlflow/blob/ba41106d110b67096384da12b6b85473457598db/mlflow/store/tracking/sqlalchemy_store.py#L7112-L7230)

| Selected experiments | Candidate SELECTs | Rows returned per SELECT | Rows kept per chunk | Median ms (min–max) |
| ---: | ---: | --- | --- | ---: |
| 1,000 | 1 | 100 | 100 | 2.68 (2.64–2.72) |
| 1,001 | 2 | 100 + 100 | 50 + 50 | 3.58 (3.52–3.73) |

Experiments 1,000 and 1,001 had traces with identical timestamps on either side of the chunk boundary. At the batch cutoff, trace ID `…0062` from experiment 1,000 ranked 100th and `…0063` from experiment 1,001 ranked 101st at the same timestamp. Every timed batch matched an independent global sort by timestamp and trace ID. Swapping the two experiments between chunks also preserved the exact order. [Fixture](../../chunk_fixture.py) · [Ordering test](../../test_chunk_study.py)

Timings cover 12 complete transactions per scope on one warm connection, alternating which scope ran first. The untimed query captures and replays verified the IDs, limits and row counts for each SELECT. [Raw samples, ordered rows and plans](../chunk-study.json.gz) · [Benchmark run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45676900586)

## Archive three traces per pass

On PostgreSQL 16.16 with local file storage, MLflow 3.18.0 took a median **22.14 ms** to archive a full batch of three traces. Selecting those traces took **3.11 ms**. Across all 210 full batches, selection accounted for **14.3%** of elapsed time. On the final empty pass it accounted for **94.6%**.

| Traces archived per pass | Measured passes | Median selection (ms) | Median complete pass (ms) |
| ---: | ---: | ---: | ---: |
| 3 | 210 | 3.11 | 22.14 |
| 2 | 7 | 2.58 | 15.14 |
| 0 | 7 | 2.51 | 2.65 |

![Candidate selection and complete archival time across 32 consecutive passes. The first full pass took 29.11 ms, the last full pass took 21.47 ms, and the empty pass took 2.65 ms.](drain-pass-times.png)

Each point is the median for that pass number across seven fresh queues. Each queue held 92 eligible traces across three experiments, with a root and child span per trace. Passes 1–30 archived three traces each, pass 31 archived the remaining two, and pass 32 confirmed the queue was empty. Selection includes experiment and retention lookups, the candidate query, and ordering. The complete pass includes selection, span serialization, local file writes, and database finalization. Readback runs after timing. [Pass timer](../../drain_measure.py) · [Per-pass measurements](../drain-study.json.gz) · [PostgreSQL run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45955942205)

The fixture logged traces in reverse archival order, with four traces sharing each timestamp. Every pass selected and successfully archived exactly the expected IDs in timestamp and trace-ID order, including ties split between passes. Across all seven queues, the benchmark verified **644 archive files** and **1,288 cleared database span payloads**. MLflow’s `get_trace` read back every root and child span, and each matched its original contents. [Fixture and readback checks](../../drain_fixture.py)

To repeat the run, [install the driver dependencies](#reproduce-these-studies) and run this command from the repository root.

```sh
.venv/bin/python drain_study.py --rounds 7 --traces 92 --output drain-study.json.gz
```

To redraw the chart from your own report, run `python3 results/finder/plot_drain.py --input drain-study.json.gz --output drain-pass-times.png` with Matplotlib installed.

### Where a three-trace pass spends its time

**Database finalization took 9.99 ms per full pass and loading the spans took 6.35 ms.** Together they accounted for **86.0% of the time outside candidate selection**. Writing the three local archive files took 1.67 ms, and serializing their contents took 0.54 ms.

The original full-pass median was **22.14 ms**. For this comparison, one PostgreSQL 16.16 runner measured the workload twice. The control used the original timers (**22.12 ms** median). The second run added four timers inside each pass (**22.23 ms** median). Each run drained seven fresh queues of 92 traces. The table gives mean times across the 210 full passes with phase timing enabled. Shares use total elapsed time across those passes. [Control measurements](../drain-phase-control.json.gz) · [Per-pass phase timings and call counts](../drain-phase-study.json.gz) · [Benchmark run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45958568812)

| Phase | Mean ms per three-trace pass | Share of elapsed time |
| --- | ---: | ---: |
| Select candidates | 3.13 | 14.1% |
| Load span rows and trace metadata, then check eligibility | 6.35 | 28.7% |
| Decode stored spans and serialize to protobuf | 0.54 | 2.4% |
| Stage the protobuf and write the local archive file | 1.67 | 7.6% |
| Recheck the trace, clear database span contents, update tags, and commit | 9.99 | 45.1% |
| Other work, including session handling outside the timers and repository setup | 0.46 | 2.1% |
| **Complete pass** | **22.15** | **100%** |

Each archived trace called all four timed operations once, in order. The seven empty passes called none. The intervals and residual account for each complete pass before rounding. All 224 profiled passes returned the same ordered IDs as the control, including ties across batch boundaries. After timing, MLflow read back all 1,288 spans from 644 archive files and matched them to the inputs. [Timing code](../../drain_measure.py) · [MLflow archival path](https://github.com/mlflow/mlflow/blob/ba41106d110b67096384da12b6b85473457598db/mlflow/store/tracking/sqlalchemy_store.py#L7232-L7481)

To repeat the breakdown, use the same setup as the drain benchmark and enable phase timing.

```sh
.venv/bin/python drain_study.py --rounds 7 --traces 92 --profile-phases --output drain-phase-study.json.gz
```

### Inside database finalization

Of the **9.48 ms** MLflow spent finalizing a three-trace pass, **2.38 ms** went to executing SQL statements and **0.35 ms** to committing transactions. The largest part, **6.75 ms (71.2%)**, elapsed inside finalization but outside the measured driver calls.

| Work inside finalization | Calls per full pass | Mean ms per full pass | Share of finalization |
| --- | ---: | ---: | ---: |
| Read and lock trace headers, read tags, check for span content, and look up the archive-location tag | 12 | 1.290 | 13.61% |
| Clear span content, update the span-location tag, insert the archive-location tag, and delete any failure tag | 12 | 1.090 | 11.50% |
| Commit | 3 | 0.348 | 3.67% |
| Reset connections on return to the pool | 3 | 0.004 | 0.04% |
| Time outside these driver calls | — | 6.751 | 71.19% |
| **Total finalization** | | **9.483** | **100%** |

The timers surround actual psycopg2 cursor executions, commits, and pool-reset calls. A reset calls `rollback` after a successful commit. The next experiment measures how much of the remaining time goes to fetching rows, building objects, and managing sessions. [Timer implementation](../../finalize_probe.py) · [Individual operation times and counts](../drain-finalization.json.gz)

Each run recreated seven 92-trace queues on PostgreSQL 16.16 with local file storage. The table covers the 210 full passes. Finalization averaged **9.84 ms** in the phase-only control on the same runner and **9.99 ms** in the earlier study. All 224 passes matched the control’s ordered trace IDs. The seven empty passes issued no finalization calls. After timing, MLflow read back all **1,288 spans from 644 archive files** and matched them to the original inputs. [Control timings](../drain-finalization-control.json.gz) · [Benchmark run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45972390735)

To reproduce the comparison, run the phase-timing command above, then repeat with driver-call timing enabled.

```sh
.venv/bin/python drain_study.py --rounds 7 --traces 92 --profile-finalization --output drain-finalization.json.gz
```

### Fetching rows, building objects, and managing sessions

Across 210 three-trace passes, **SQLAlchemy session handling took 2.71 ms per pass**. Fetching results and constructing Python rows and mapped objects together took **0.21 ms**. The largest share, **4.31 ms**, remained outside the timed methods.

| Work after excluding driver executions, commits, and resets | Timed calls per full pass | Mean ms per full pass | Share of time outside those driver calls |
| --- | ---: | ---: | ---: |
| Fetch rows from the cursor and release it | 12 | 0.030 | 0.4% |
| Wrap rows, load or allocate mapped objects, and construct trace tags | 39 | 0.176 | 2.4% |
| Open and close sessions, begin transactions, acquire connections, merge tags, flush changes, and commit | 66 | 2.712 | 37.5% |
| Other finalization work | — | 4.309 | 59.6% |
| **Total outside the measured driver calls** | | **7.227** | **100%** |

The fetch timer starts when SQLAlchemy asks the buffered psycopg2 cursor for rows. Object timing covers the Python work that turns those rows into SQLAlchemy rows and model instances, plus explicit trace-tag construction. A session commit can trigger a flush, which can execute SQL. Driver calls keep their own time, and each remaining interval goes to the innermost timed method. The call counts include nested methods. The unassigned time includes work such as constructing queries outside those methods and timer bookkeeping. [SQLAlchemy timers](../../orm_probe.py) · [Timing calculation](../../finalize_probe.py)

On the same PostgreSQL 16.16 runner, the driver-only control averaged **7.09 ms** outside driver calls, compared with **7.23 ms** with the additional timers and **6.75 ms** in the earlier study. Median complete passes took **22.20 ms** in the control and **22.53 ms** with the additional timers. Both runs returned the same ordered IDs across all 224 passes. Each verified all **1,288 spans from 644 archives** against the inputs after clearing the database payloads. All seven empty passes recorded zero finalization calls. [Profiled passes](../drain-orm.json.gz) · [Same-runner control](../drain-orm-control.json.gz) · [Benchmark run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/45994588581)

To reproduce the table, run the driver-only command above for the control, then enable the SQLAlchemy timers.

```sh
.venv/bin/python drain_study.py --rounds 7 --traces 92 --profile-orm --output drain-orm.json.gz
```

### Building, compiling, and dispatching queries

**Building queries took 1.20 ms per three-trace pass. Generating SQL took 0.028 ms.** SQLAlchemy found a compiled statement on 4,998 of 5,040 lookups across the 210 full passes. Each queue’s first pass issued six compilations. SQL generation accounted for 0.85 ms of previously unassigned time on those first passes and zero on every later pass.

| Work inside the previously unassigned time | Mean ms per full pass | Share |
| --- | ---: | ---: |
| Build queries, predicates, loader options, and statement objects | 1.204 | 27.1% |
| Build cache keys, look up compiled statements, and store new entries | 0.855 | 19.3% |
| Compile statements into SQL | 0.028 | 0.6% |
| Dispatch statements through SQLAlchemy | 0.829 | 18.7% |
| Outside all timed methods | 1.523 | 34.3% |
| **Total** | **4.440** | **100%** |

Dispatch covers processing execution options, selecting a database connection or engine, and calling `Connection.execute` through cursor-result setup. Driver calls, fetches, row and model construction, and session handling keep their own time. Each remaining interval counts once, even when a cache miss invokes the compiler inside a dispatch call. [Query timers](../../query_probe.py) · [Timing calculation](../../finalize_probe.py)

The earlier study left **4.31 ms** unassigned. A new PostgreSQL 16.16 run measured **4.22 ms** with the previous timers and **4.44 ms** with query timing enabled on the same runner. The four measured query operations accounted for **2.92 ms (65.7%)** of that 4.44 ms. Median complete passes took **22.40 ms** in the control and **23.39 ms** with query timing. [Per-pass measurements and cache counts](../drain-queries.json.gz) · [Same-runner control](../drain-queries-control.json.gz)

Both runs matched the expected trace IDs on all 224 passes, including timestamp ties split across batches. In each run, MLflow read back all 1,288 root and child spans from 644 archives after clearing the database payloads. Every span matched its input. All seven empty passes recorded zero finalization queries. [PostgreSQL benchmark run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/46056302730)

To reproduce the comparison, run the control and then enable query timing.

```sh
.venv/bin/python drain_study.py --rounds 7 --traces 92 --profile-orm --output drain-orm.json.gz
.venv/bin/python drain_study.py --rounds 7 --traces 92 --profile-queries --output drain-queries.json.gz
```

### Eligibility checks and result processing

**SQLAlchemy result processing accounted for two-thirds of the remaining finalization time.** Across 210 three-trace passes on PostgreSQL 16.16, it took **1.012 ms** per pass. MLflow’s eligibility rechecks took **0.113 ms**. Together they accounted for **74.1%** of the remainder.

| Work inside the query-timing remainder | Timed calls per full pass | Mean ms per full pass | Share of remainder |
| --- | ---: | ---: | ---: |
| MLflow eligibility rechecks | 6 | 0.113 | 7.5% |
| SQLAlchemy result processing | 42 | 1.012 | 66.6% |
| Outside all timed methods | — | 0.394 | 25.9% |
| **Total** | | **1.520** | **100%** |

For each archived trace, eligibility timing starts when MLflow’s locked header query returns. MLflow checks that the header exists, the payload generation is unchanged, spans are database-backed, the trace has no archival failure tag, and its status permits archival. It then checks for nonempty span content. SQLAlchemy result timing covers setting up SELECT and bulk UPDATE/DELETE results, applying scalar and uniqueness filters, and consuming single rows, including the work that triggers the related tag query. Nested query, driver, fetch, object, and session calls keep their own time. [MLflow finalizer](https://github.com/mlflow/mlflow/blob/ba41106d110b67096384da12b6b85473457598db/mlflow/store/tracking/sqlalchemy_store.py#L7423-L7480) · [Timers](../../result_probe.py)

The earlier study left **1.523 ms** unassigned. We repeated the workload twice on one runner. The query-only control measured a remainder of **1.487 ms**. With the additional timers, the same remainder was **1.520 ms** before the breakdown above. Median complete passes took **22.44 ms** in the control and **22.52 ms** with the new timers. [Control measurements](../drain-results-control.json.gz) · [Per-pass breakdown](../drain-results.json.gz)

Each run matched the expected timestamp and trace-ID order on all **224 passes**, including ties across batches. After the database span payloads were cleared, MLflow read back **1,288 spans from 644 archives** in each run. Every root and child span matched its input. The seven empty passes recorded zero calls to either set of timers. [Benchmark run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/46103725854)

To reproduce the comparison, run the query-only control and then enable result timing.

```sh
.venv/bin/python drain_study.py --rounds 7 --traces 92 --profile-queries --output drain-queries.json.gz
.venv/bin/python drain_study.py --rounds 7 --traces 92 --profile-results --output drain-results.json.gz
```

### Result setup, filtering, and timer overhead

**Consuming rows took 0.705 ms per three-trace pass, 57.3% of SQLAlchemy result processing.** The earlier run measured 1.012 ms for all result processing. The new control measured 1.234 ms. Giving the same 42 calls more specific labels, using the same clocks, yielded 1.231 ms across 210 full passes.

| SQLAlchemy result operation | Calls per full pass | Mean ms per full pass | Share of result time |
| --- | ---: | ---: | ---: |
| Set up SELECT results and adapt bulk UPDATE/DELETE results | 18 | 0.510 | 41.4% |
| Configure scalar and uniqueness filters | 15 | 0.016 | 1.3% |
| Consume single rows, including uniqueness checks and eager tag loading | 9 | 0.705 | 57.3% |

The filtering timer covers calls to `scalars()` and `unique()`. Applying those filters as rows are read counts toward consumption. The timings exclude nested work already assigned to other operations. Each pass reconciles to its result-processing total before rounding. [Result timers](../../result_probe.py) · [Per-pass breakdown](../result-timing/detail.json.gz)

| Timing mode | Mean result processing (ms) | Mean time outside the prior timers (ms) | Median complete pass (ms) |
| --- | ---: | ---: | ---: |
| Original result timer | 1.234 | 0.500 | 30.34 |
| Three result categories, same clocks | 1.231 | 0.506 | 30.35 |
| Three categories plus timers around existing wrappers | 1.226 | 0.595 | 30.90 |

**Timer entry and exit accounted for 0.101 ms (17.0%) of the 0.595 ms remainder in the run that measured them.** The outer timers capture wrapper entry, forwarding, and recording the original timer’s event. Nested intervals count once. The remaining **0.494 ms** includes other work and the outer timers’ own event recording wherever it falls outside all timed intervals. Enabling the outer timers increased the mean remainder by **0.089 ms** compared with the three-category run. The bookkeeping share of the earlier **0.394 ms** remains uncertain because observing it changed the timing. [Bookkeeping timer](../../timer_overhead.py) · [Control](../result-timing/control.json.gz) · [Overhead measurements](../result-timing/overhead.json.gz)

All three modes ran on the same PostgreSQL 16.16 runner. Each round recreated the same 92-trace queue for each mode. The first six rounds covered all six execution orders, and the seventh repeated the first order. Each mode matched the expected IDs on all **224 passes**, including ties across batches, and read back all **1,288 spans from 644 archives** after the database payloads were cleared. Every span matched its input. [Comparison runner](../../result_timing_study.py) · [Benchmark run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/46114148244)

This command runs all three modes and saves their per-pass reports.

```sh
.venv/bin/python result_timing_study.py --rounds 7 --traces 92 --output-dir result-timing
```

### Row iteration, uniqueness checks, and eager tag loading

**Loading the locked traces’ tags accounted for 78.4% of row-consumption time.** Across 210 three-trace passes on PostgreSQL 16.16, SQLAlchemy spent 0.599 ms per pass on that work after excluding time already assigned to SQL execution, fetching, and other measured operations.

| Work inside row consumption | Calls per full pass | Mean ms per full pass | Share |
| --- | ---: | ---: | ---: |
| Advance the single-row iterator | 12 | 0.071 | 9.3% |
| Check uniqueness in the single-row consumer | 3 | 0.012 | 1.5% |
| Eagerly load and attach trace tags | 3 | 0.599 | 78.4% |
| Other row-consumer work and timer bookkeeping | — | 0.082 | 10.7% |
| **Total row consumption** | | **0.764** | **100%** |

On each full pass, six iterator advances returned a row and six reached the end of a result. The uniqueness check prepared the first row’s identity and looked for a second row. Across all 630 checks, zero additional rows required an equality comparison. The time spent advancing the iterator during that check appears in the iteration row. [SQLAlchemy’s single-row consumer](https://github.com/sqlalchemy/sqlalchemy/blob/ce50c07cc877d5e78ad058da300bc9a22a7e13ed/lib/sqlalchemy/engine/result.py#L788-L871) · [Timing code](../../consumption_probe.py)

MLflow requests the trace’s tags when it loads the locked header. Each tag load issued exactly one SELECT. The three loads together took **1.784 ms** end to end, including **0.281 ms** executing those SELECTs in the driver. The 0.599 ms in the table is the portion charged to row consumption. It includes iterating the separate tag result and attaching the tags to the header. [MLflow finalizer](https://github.com/mlflow/mlflow/blob/ba41106d110b67096384da12b6b85473457598db/mlflow/store/tracking/sqlalchemy_store.py#L7423-L7480) · [Per-pass measurements](../row-consumption/detail.json.gz)

The earlier study measured **0.705 ms** for row consumption. On this runner the control measured **0.737 ms** and the detailed timers measured **0.764 ms**, a difference of **0.027 ms** per full pass. Median complete passes took **28.67 ms** and **28.95 ms**, respectively. The two modes alternated which ran first across seven fresh queues each. Both matched the expected timestamp and trace-ID order on all **224 passes**, including ties across batches. In each mode, MLflow read back all **1,288 spans from 644 archives** after the database payloads were cleared. Every span matched its input. [Same-runner control](../row-consumption/control.json.gz) · [Benchmark run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/46116568768)

Run the matched comparison and save both reports with this command.

```sh
.venv/bin/python result_timing_study.py --study consumption --rounds 7 --traces 92 --output-dir row-consumption
```

### Preparing tag queries, grouping rows, and attaching collections

**Preparing the three tag queries took 0.283 ms per full pass.** That was 57.0% of the time charged to eager tag loading and more than twice the time spent grouping six returned tag rows and attaching them to three trace headers. The table gives means across 210 three-trace passes on PostgreSQL 16.16.

| Work inside eager tag loading | Mean ms per full pass | Share of exclusive loader time |
| --- | ---: | ---: |
| Prepare the tag SELECT and its parent keys | 0.283 | 57.0% |
| Group returned tag rows by trace ID | 0.091 | 18.3% |
| Attach each tag collection to its trace header | 0.041 | 8.2% |
| Other loader work and timer bookkeeping | 0.082 | 16.6% |
| **Total** | **0.497** | **100%** |

Each query loaded two tags for one header. Preparation ends before `Session.execute`. Grouping starts after execution returns and SQLAlchemy configures the result’s uniqueness filter. Attachment looks up the group for each header and sets its loaded collection. The table excludes work already charged to SQL execution, fetching, and other earlier timers. Including that nested work, the three tag loads took **1.490 ms** end to end. [SQLAlchemy loader](https://github.com/sqlalchemy/sqlalchemy/blob/ce50c07cc877d5e78ad058da300bc9a22a7e13ed/lib/sqlalchemy/orm/strategies.py#L3165-L3434) · [Timers](../../tag_load_probe.py) · [Per-pass breakdown](../tag-load/detail.json.gz)

The earlier study measured **0.599 ms** of exclusive loader time. We repeated the workload in two modes on one runner. The control measured **0.478 ms** and the detailed timers measured **0.497 ms**. Median complete passes took **23.10 ms** and **22.97 ms**, respectively. The modes alternated which ran first across seven matched pairs of 92-trace queues. [Control measurements](../tag-load/control.json.gz) · [Benchmark run](https://github.com/kenchengkc/mlflow-trace-archive-bench/actions/runs/46117667862)

Both modes matched the expected timestamp and trace-ID order on all **224 passes**, including ties across batches. Each read back all **1,288 spans from 644 archives** after the database payloads were cleared. Every span matched its input. A separate test loaded three headers in one tag query and verified the correct collection for headers with two tags, one tag, and no tags. [Ordering, grouping, and readback tests](../../test_tag_load.py)

```sh
.venv/bin/python result_timing_study.py --study tag-load --rounds 7 --traces 92 --output-dir tag-load
```

## Reproduce these studies

Run the commands from the repository root. The runner uses a disposable PostgreSQL cluster and initializes the tracking tables with MLflow’s migrations. [PostgreSQL prerequisites](../../README.md#reproduce-the-benchmark)

Install the pinned MLflow release and driver, then compare the finder, raw SQL and a replay of MLflow’s SQL on all three queues.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-driver.txt
.venv/bin/python driver_study.py --output driver-study.json.gz
```

Verify the two-step selector under concurrent writes, then reproduce its three published comparisons. The first run selects all eight experiments, the second selects the rare experiment, and the third uses the fresh million-trace queue.

```sh
.venv/bin/python -m unittest -v test_two_step
.venv/bin/python two_step_study.py --output two-step.json.gz
.venv/bin/python two_step_study.py --scope 8 --output two-step-rare.json.gz
.venv/bin/python two_step_study.py --rows 1000000 --fresh-only --output two-step-million-fresh.json.gz
```

To compare 1,000 and 1,001 selected experiments on the same database and verify the merged order, run the chunk study.

```sh
.venv/bin/python chunk_study.py --output chunk-study.json.gz
```
