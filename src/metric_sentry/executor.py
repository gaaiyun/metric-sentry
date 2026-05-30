"""Metric executor.

Turns a :class:`~metric_sentry.contracts.Metric` into SQL and runs it through
SQLAlchemy. DuckDB is the only engine wired up for the MVP, but the executor
only depends on a SQLAlchemy :class:`~sqlalchemy.engine.Engine`, so swapping in
Postgres/Snowflake/BigQuery later is a matter of building a different URL.

For each metric we compute two things:

* the **overall** aggregate value, and
* a **per-dimension breakdown** (one value per distinct combination of the
  metric's ``dimensions``), which lets the diff engine point at *which* segment
  moved rather than just reporting that the headline number changed.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, text

from metric_sentry.contracts import Metric, MetricSuite

# Sentinel used in dimension keys when a grouping column is SQL NULL.
NULL_LABEL = "∅"  # the empty-set symbol, hard to collide with real data


@dataclass
class MetricResult:
    """Computed value of a single metric at one point in time."""

    name: str
    value: float | None
    row_count: int
    breakdown: dict[str, float | None] = field(default_factory=dict)
    fingerprint: str = ""
    grain: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "row_count": self.row_count,
            "breakdown": self.breakdown,
            "fingerprint": self.fingerprint,
            "grain": self.grain,
        }


def duckdb_engine(database: str | Path | None) -> Engine:
    """Create a SQLAlchemy engine for a DuckDB database.

    ``None`` or ``":memory:"`` opens a transient in-memory database, which is
    handy for tests. Otherwise the path is opened (and created if absent).
    """
    if database is None or str(database) == ":memory:":
        return create_engine("duckdb:///:memory:")
    path = Path(database)
    return create_engine(f"duckdb:///{path}")


def _where_clause(filters: list[str]) -> str:
    if not filters:
        return ""
    joined = " AND ".join(f"({f})" for f in filters)
    return f" WHERE {joined}"


def build_overall_sql(metric: Metric) -> str:
    """Render the SQL that produces the metric's headline value + row count."""
    return (
        f"SELECT ({metric.expression}) AS value, "
        f"count(*) AS row_count "
        f"FROM {metric.source}{_where_clause(metric.filters)}"
    )


def build_breakdown_sql(metric: Metric) -> str:
    """Render the SQL that produces one value per dimension combination."""
    dims = ", ".join(metric.dimensions)
    return (
        f"SELECT {dims}, ({metric.expression}) AS value "
        f"FROM {metric.source}{_where_clause(metric.filters)} "
        f"GROUP BY {dims} "
        f"ORDER BY {dims}"
    )


def _coerce(value) -> float | None:
    """Normalise DB numeric types (Decimal, int) to float for stable JSON."""
    if value is None:
        return None
    return float(value)


def _dim_key(values: tuple) -> str:
    parts = [NULL_LABEL if v is None else str(v) for v in values]
    return " | ".join(parts)


def run_metric(engine: Engine, metric: Metric) -> MetricResult:
    """Compute one metric against ``engine`` and return its value + breakdown."""
    with engine.connect() as conn:
        row = conn.execute(text(build_overall_sql(metric))).one()
        value = _coerce(row.value)
        row_count = int(row.row_count)

        breakdown: dict[str, float | None] = {}
        if metric.dimensions:
            result = conn.execute(text(build_breakdown_sql(metric)))
            n_dims = len(metric.dimensions)
            for record in result:
                dim_values = tuple(record[:n_dims])
                breakdown[_dim_key(dim_values)] = _coerce(record[-1])

    return MetricResult(
        name=metric.name,
        value=value,
        row_count=row_count,
        breakdown=breakdown,
        fingerprint=metric.definition_fingerprint(),
        grain=metric.grain,
    )


def run_suite(suite: MetricSuite, engine: Engine | None = None) -> list[MetricResult]:
    """Compute every metric in ``suite``.

    If no engine is supplied, one is built from ``suite.database`` (DuckDB). The
    engine is disposed afterwards when we own it.
    """
    owned = engine is None
    eng = engine or duckdb_engine(suite.database)
    try:
        return [run_metric(eng, m) for m in suite.metrics]
    finally:
        if owned:
            eng.dispose()


@contextmanager
def open_engine(suite: MetricSuite) -> Iterator[Engine]:
    """Context manager yielding an engine for ``suite`` and disposing it."""
    eng = duckdb_engine(suite.database)
    try:
        yield eng
    finally:
        eng.dispose()
