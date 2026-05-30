# metric-sentry

Test your business metrics like code. metric-sentry snapshots the *definition*
and the *computed value* of each metric, then diffs them on every change, so a
quiet edit to a join or filter that drops MRR by 12% gets caught in code review
instead of in next month's board deck.

It is `pytest` + `git diff` + a CI gate for your KPIs. It fills the gap left when
[Datafold's open-source `data-diff`](https://github.com/datafold/data-diff) was
archived in 2024: that tool diffed *rows between tables*; metric-sentry diffs the
*aggregated numbers your metrics actually produce* and the SQL that produces them.

## Why

A metric like MRR is a small program: a table, a filter, an aggregate. People
edit that program constantly — add a `WHERE`, swap a join, change the grain — and
the number silently moves. Nothing fails. The dashboard just quietly becomes
wrong, and you find out weeks later.

metric-sentry treats the metric as code under test:

- The metric is defined in a **contract** (YAML): grain, filters, expression,
  tolerance, owner.
- `run` computes every metric and writes a **golden snapshot** (JSON, committed
  to git, reviewed like any other file).
- `diff` recomputes and compares against golden: how much did the value move, in
  which dimension, and did the **definition** itself change.
- `ci` does the same and **exits non-zero** when a metric breaches its tolerance,
  so a pull request can be blocked before it merges.

## 30-second start

Requires Python 3.10+. The sample project runs fully offline on a bundled DuckDB
database — no warehouse, no credentials.

```bash
pip install metric-sentry          # or: pip install -e .  (from a clone)

metric-sentry init demo            # writes metrics.yml + a sample DuckDB
cd demo

metric-sentry run                  # compute metrics, write the golden snapshot
metric-sentry diff                 # recompute and compare against golden
```

`init` lays down a contract and a 600-row `orders` table; `run` snapshots the
metrics; `diff` shows the comparison. That is the whole loop.

## What it looks like

`metric-sentry run` computes each metric and freezes the values into a snapshot:

```
suite: saas_sample   (3 metrics)
  mrr                =           26,243   [rows=267, owner=revenue-team]
  active_users       =              232   [rows=462, owner=growth-team]
  conversion_rate    =           0.5083   [rows=600, owner=growth-team]
wrote snapshot -> snapshots/golden.json
```

Run `metric-sentry diff` with nothing changed and everything is green:

```
suite: saas_sample
OK mrr                        26,243 -> 26,243         (+0.00%)  [ok]
OK active_users                  232 -> 232            (+0.00%)  [ok]
OK conversion_rate            0.5083 -> 0.5083         (+0.00%)  [ok]

no breaches: all metrics within tolerance
```

Now suppose someone edits the MRR contract and adds `plan != 'enterprise'` to the
filters — the kind of one-line change that sails through review. `metric-sentry ci`
catches it, names the dimensions that moved, and fails the build:

```
suite: saas_sample
## mrr                        26,243 -> 12,770         (-51.34%)  [definition_changed]
      metric definition changed (join/filter/expression/grain). Review the contract diff: the number is no longer comparable.
      - APAC                8,279 -> 4,287        (-3,992)
      - EU                 10,372 -> 4,384        (-5,988)
      - NA                  7,592 -> 4,099        (-3,493)
OK active_users                  232 -> 232            (+0.00%)  [ok]
OK conversion_rate            0.5083 -> 0.5083         (+0.00%)  [ok]

1 breach(es): mrr
```

`ci` exits 1 here, so the pull request is blocked until the change is justified
(and the golden snapshot deliberately regenerated). A pure value drift beyond the
declared tolerance — same definition, different data — produces a `breach` verdict
the same way.

## The contract

A contract is a YAML file describing one or more metrics. This is the sample
`init` writes:

```yaml
suite: saas_sample
database: sample.duckdb

metrics:
  - name: mrr
    description: Monthly recurring revenue from active paid subscriptions.
    owner: revenue-team
    source: orders
    grain: one row per subscription order
    expression: sum(mrr)
    filters:
      - status = 'active'
      - is_paid = true
    dimensions: [region]
    tolerance:
      pct: 0.05          # MRR may drift 5% before we block a PR

  - name: conversion_rate
    description: Share of signups on a paid plan.
    owner: growth-team
    source: orders
    grain: one row per subscription order
    expression: count(*) FILTER (WHERE is_paid) * 1.0 / count(*)
    dimensions: [region]
    tolerance:
      pct: 0.02
```

Field reference:

| Field         | Meaning |
|---------------|---------|
| `source`      | Table or view the metric reads from. |
| `grain`       | The row grain of the source (documented and snapshotted, so a grain change is visible in the diff). |
| `expression`  | A SQL aggregate that yields the value, e.g. `sum(mrr)`, `count(distinct user_id)`. |
| `filters`     | SQL predicates AND-ed together to scope the rows. |
| `dimensions`  | Columns to break the metric down by, so a diff can point at *which* segment moved. |
| `tolerance`   | `abs: N` (absolute) or `pct: F` (fraction, `0.05` = 5%). Omit it for zero tolerance: any change is a breach. |
| `owner`       | Who is responsible when it breaks. Cosmetic — does not affect the value or the definition fingerprint. |

Each metric carries a **definition fingerprint**: a hash of `source`, `grain`,
`expression`, `filters`, and `dimensions` (filter order is normalised; `owner`
and `description` are ignored). When that fingerprint changes between snapshots,
the diff reports `definition_changed` regardless of how little the value moved —
because once the definition changes, the old and new numbers are not comparable.

## How the value is computed

For each metric, metric-sentry renders SQL through SQLAlchemy and runs it on the
configured engine:

```sql
SELECT (sum(mrr)) AS value, count(*) AS row_count
FROM orders
WHERE (status = 'active') AND (is_paid = true);
```

and, when `dimensions` are set, a second grouped query for the per-segment
breakdown. DuckDB is the engine wired up today; because everything goes through a
SQLAlchemy engine, other warehouses are a connection-string change away (see the
roadmap).

## Commands

| Command | What it does |
|---------|--------------|
| `metric-sentry init [DIR]` | Scaffold a runnable sample project: a contract plus a DuckDB sample database. |
| `metric-sentry run` | Compute every metric and write a golden snapshot (`--contract`, `--out`). |
| `metric-sentry diff` | Recompute and diff against the golden snapshot; report only (`--baseline`, `--json`, `--no-dimensions`). |
| `metric-sentry ci` | Same as `diff`, but exit non-zero on a breach so a PR gate can block. |

`new` and `removed` metrics are reported but do not, by themselves, fail `ci`;
only out-of-tolerance value moves and definition changes do.

## Use it in CI

A ready-to-copy GitHub Actions workflow lives at
[`.github/workflows/metric-sentry.yml`](.github/workflows/metric-sentry.yml). The
gist:

```yaml
- run: pip install metric-sentry
- run: metric-sentry ci --contract metrics.yml --baseline snapshots/golden.json
```

Commit the golden snapshot to the repo and review it like code. When a metric
change is intended, regenerate the snapshot on purpose with `metric-sentry run`
and commit the new values alongside the contract change — the reviewer sees both
in the same diff.

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q
```

The test suite covers contract parsing and validation, SQL generation, the
executor against the sample data, snapshot round-tripping, and the full
tolerance/diff matrix including the silent-filter-change scenario.

## Roadmap

This release does one thing properly: contract → snapshot → tolerance-aware diff
→ CI gate, on DuckDB, with the headline regression scenarios covered by tests.
Deliberately not yet built:

- **More engines.** The executor only depends on a SQLAlchemy engine; Postgres,
  Snowflake, BigQuery, and DuckDB-over-MotherDuck need connection wiring and an
  engine-selection layer in the contract, plus dialect testing.
- **Parquet snapshots end to end.** A Parquet writer for dimension breakdowns
  ships today (`metric_sentry.snapshot.write_breakdown_parquet`), but JSON is the
  only format `run`/`diff` read; a `--format parquet` round trip is next.
- **Time-grain metrics.** Snapshotting a metric *per period* (per month/week) and
  diffing the series, rather than a single headline number.
- **Richer CI reporting.** A Markdown summary posted as a PR comment, and a JUnit
  XML report so the breach shows up in the checks UI.
- **Auto-regenerate guard.** Detect when a snapshot was regenerated in the same
  PR that changed a definition, and require an explicit acknowledgement.

If you need one of these, the issue tracker is the place.

## License

MIT — see [LICENSE](LICENSE).
