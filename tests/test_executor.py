"""Tests for the SQLAlchemy/DuckDB executor against the sample data."""

from __future__ import annotations

from metric_sentry.contracts import Metric, MetricSuite
from metric_sentry.executor import (
    build_breakdown_sql,
    build_overall_sql,
    duckdb_engine,
    run_metric,
    run_suite,
)


def test_build_overall_sql_no_filters():
    m = Metric(name="m", source="orders", grain="g", expression="sum(mrr)")
    sql = build_overall_sql(m)
    assert "FROM orders" in sql
    assert "WHERE" not in sql
    assert "(sum(mrr)) AS value" in sql


def test_build_overall_sql_with_filters():
    m = Metric(
        name="m",
        source="orders",
        grain="g",
        expression="sum(mrr)",
        filters=["status = 'active'", "is_paid = true"],
    )
    sql = build_overall_sql(m)
    assert "WHERE (status = 'active') AND (is_paid = true)" in sql


def test_build_breakdown_sql():
    m = Metric(
        name="m",
        source="orders",
        grain="g",
        expression="sum(mrr)",
        dimensions=["region", "plan"],
    )
    sql = build_breakdown_sql(m)
    assert "GROUP BY region, plan" in sql
    assert "ORDER BY region, plan" in sql


def test_run_suite_sample_values(sample_suite: MetricSuite):
    results = {r.name: r for r in run_suite(sample_suite)}

    # Deterministic seed -> exact, documented values.
    assert results["mrr"].value == 26243.0
    assert results["active_users"].value == 232.0
    assert abs(results["conversion_rate"].value - 0.5083333333333333) < 1e-9


def test_run_metric_breakdown(sample_suite: MetricSuite):
    mrr = sample_suite.metric("mrr")
    engine = duckdb_engine(sample_suite.database)
    try:
        res = run_metric(engine, mrr)
    finally:
        engine.dispose()

    # Region breakdown sums back to the overall value.
    assert set(res.breakdown) == {"NA", "EU", "APAC"}
    assert abs(sum(res.breakdown.values()) - res.value) < 1e-6


def test_run_metric_carries_fingerprint_and_grain(sample_suite: MetricSuite):
    mrr = sample_suite.metric("mrr")
    engine = duckdb_engine(sample_suite.database)
    try:
        res = run_metric(engine, mrr)
    finally:
        engine.dispose()
    assert res.fingerprint == mrr.definition_fingerprint()
    assert res.grain == mrr.grain


def test_filter_change_moves_value(sample_suite: MetricSuite):
    """Adding a filter that drops a plan must lower MRR (the regression we catch)."""
    engine = duckdb_engine(sample_suite.database)
    try:
        full = run_metric(engine, sample_suite.metric("mrr"))
        tampered = Metric(
            name="mrr",
            source="orders",
            grain="one row per subscription order",
            expression="sum(mrr)",
            filters=["status = 'active'", "is_paid = true", "plan != 'enterprise'"],
        )
        dropped = run_metric(engine, tampered)
    finally:
        engine.dispose()

    assert dropped.value < full.value
    assert dropped.fingerprint != full.fingerprint


def test_null_value_when_no_rows_match():
    """sum() over an empty set is NULL -> executor returns None, not a crash."""
    engine = duckdb_engine(":memory:")
    try:
        with engine.begin() as conn:
            from sqlalchemy import text

            conn.execute(text("CREATE TABLE t (x INTEGER)"))
            conn.execute(text("INSERT INTO t VALUES (1), (2)"))
        m = Metric(
            name="m", source="t", grain="g", expression="sum(x)", filters=["x > 100"]
        )
        res = run_metric(engine, m)
    finally:
        engine.dispose()
    assert res.value is None
    assert res.row_count == 0
