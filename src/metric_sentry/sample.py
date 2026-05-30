"""Self-contained sample dataset so the CLI runs offline out of the box.

Builds a small but realistic SaaS-style ``orders`` table in DuckDB. The numbers
are deterministic (fixed seed) so the golden snapshot is stable across machines
and the docs can quote exact values. The shipped sample suite computes three
classic metrics off this table:

* **MRR**        -- sum of monthly recurring revenue for active subscriptions
* **active_users** -- distinct users with at least one active subscription
* **conversion_rate** -- paid signups / all signups
"""

from __future__ import annotations

import random
from pathlib import Path

from sqlalchemy import text

from metric_sentry.executor import duckdb_engine

PLANS = {
    "free": 0.0,
    "starter": 29.0,
    "pro": 99.0,
    "enterprise": 499.0,
}
REGIONS = ["NA", "EU", "APAC"]


def _generate_rows(n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    rows: list[dict] = []
    for i in range(1, n + 1):
        region = rng.choice(REGIONS)
        # Plan mix: lots of free, fewer high tiers.
        plan = rng.choices(
            population=list(PLANS),
            weights=[50, 25, 18, 7],
            k=1,
        )[0]
        is_paid = plan != "free"
        # Most paid subs are active; a minority churned.
        status = "active" if (is_paid and rng.random() > 0.12) else (
            "active" if (not is_paid and rng.random() > 0.30) else "churned"
        )
        rows.append(
            {
                "order_id": i,
                "user_id": 1000 + rng.randint(0, n // 2),  # some repeat users
                "region": region,
                "plan": plan,
                "is_paid": is_paid,
                "status": status,
                "mrr": PLANS[plan],
                "signup_month": f"2026-{rng.randint(1, 5):02d}",
            }
        )
    return rows


def build_sample_db(path: str | Path, rows: int = 600, seed: int = 7) -> Path:
    """Create (or overwrite) a DuckDB file with the sample ``orders`` table.

    Returns the path written. Safe to call repeatedly; the table is replaced.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = _generate_rows(rows, seed)

    engine = duckdb_engine(p)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS orders"))
            conn.execute(
                text(
                    """
                    CREATE TABLE orders (
                        order_id      INTEGER,
                        user_id       INTEGER,
                        region        VARCHAR,
                        plan          VARCHAR,
                        is_paid       BOOLEAN,
                        status        VARCHAR,
                        mrr           DOUBLE,
                        signup_month  VARCHAR
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO orders
                    (order_id, user_id, region, plan, is_paid, status, mrr, signup_month)
                    VALUES
                    (:order_id, :user_id, :region, :plan, :is_paid, :status, :mrr, :signup_month)
                    """
                ),
                data,
            )
    finally:
        engine.dispose()
    return p


SAMPLE_CONTRACT_YAML = """\
# metric-sentry sample contract.
# Three classic SaaS metrics computed off the bundled `orders` table.
# `metric-sentry run` snapshots these; `metric-sentry diff` catches drift.
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

  - name: active_users
    description: Distinct users with at least one active subscription.
    owner: growth-team
    source: orders
    grain: one row per subscription order
    expression: count(distinct user_id)
    filters:
      - status = 'active'
    dimensions: [region]
    tolerance:
      abs: 10            # +/-10 users is noise

  - name: conversion_rate
    description: Share of signups on a paid plan.
    owner: growth-team
    source: orders
    grain: one row per subscription order
    expression: count(*) FILTER (WHERE is_paid) * 1.0 / count(*)
    dimensions: [region]
    tolerance:
      pct: 0.02          # conversion is sensitive; 2% drift max
"""


def write_sample_contract(path: str | Path) -> Path:
    """Write the bundled sample contract YAML to ``path``."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SAMPLE_CONTRACT_YAML, encoding="utf-8")
    return p
