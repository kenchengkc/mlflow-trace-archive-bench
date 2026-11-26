# Contributing

An independent rerun, a trace ordering case, or a correction to a study guide is a useful contribution. Run the commands below from the repository root.

## First check

With Python 3.10+ you can test the report reader against the published measurements. One test regenerates the 45-case timing table and checks it against the saved Markdown.

```sh
python3 -m unittest -v test_summarize
```

## Install PostgreSQL

On macOS with Homebrew, install PostgreSQL 16 and add its server tools to your current shell.

```sh
brew install postgresql@16
export PATH="$(brew --prefix postgresql@16)/bin:$PATH"
```

On Ubuntu 24.04, use the distribution packages.

```sh
sudo apt update
sudo apt install postgresql-16 postgresql-contrib
export PATH="/usr/lib/postgresql/16/bin:$PATH"
```

Run `initdb --version`, `pg_ctl --version`, and `psql --version` to check that all three use the intended PostgreSQL installation. Run the benchmarks as your normal, non-root user. They start a private temporary cluster and remove it on exit.

## Working on a query

Start with the SQL strategy tests for changes to the queries in the reduced schema.

```sh
python3 -m unittest -v test_strategies
```

For changes to the MLflow finder or ingestion benchmarks, install the pinned driver dependencies. These studies exercise MLflow’s tracking store against tables created by its migrations. The full suite also uses PostgreSQL’s `pg_stat_statements` extension for the span-upsert tests.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-driver.txt
.venv/bin/python -m unittest -v
```

## Sharing a result

- **Report an observation.** Include the command, PostgreSQL and Python versions, workload settings, and result JSON. For a comparison, run both queries on the same fixture and identify what the timing includes. [Open an issue](https://github.com/kenchengkc/mlflow-trace-archive-bench/issues/new)
- **Change a query.** Include a small test with the expected trace IDs in order. A timestamp tie at the last place in a batch can expose a bug that a row-count check would miss. In the pull request, explain the change and list the checks you ran. [Ordering tests](test_strategies.py) · [Pull requests](https://github.com/kenchengkc/mlflow-trace-archive-bench/pulls)

The study guides give the exact commands for each published workload. [SQL strategies](https://kenchengkc.github.io/mlflow-trace-archive-bench/guides/strategy-study/#replay) · [MLflow finder](https://kenchengkc.github.io/mlflow-trace-archive-bench/guides/finder/#reproduce-these-studies) · [Trace ingestion](https://kenchengkc.github.io/mlflow-trace-archive-bench/guides/ingestion/#reproduce-the-studies) · [Project roadmap](ROADMAP.md)

## Preview the study guides

Edit the README in `results/strategy-study/`, `results/finder/`, or `results/ingestion/`. GitHub Pages rebuilds the guide and its page outline when the change reaches `main`.

To preview the site locally, use Node.js 24 and Python 3.

```sh
npm ci --prefix docs
npm run build --prefix docs
node docs/check-site.mjs
python3 -m http.server --directory _site 8000
```

Open [the local site](http://localhost:8000/) and select a guide from the sidebar.
