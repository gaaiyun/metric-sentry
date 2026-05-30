"""Metric contracts.

A *contract* is the executable definition of a business metric: what table it
reads, how it is aggregated (grain), which rows it keeps (filters), the SQL
expression that produces the number, and how much that number is allowed to move
between snapshots before we treat the change as a regression (tolerance).

Contracts are authored in YAML and parsed into the Pydantic models below. The
parsed model is the single source of truth used by the executor, the snapshot
writer and the diff engine.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Tolerance(BaseModel):
    """How much a metric value may move before a diff is treated as a breach.

    Exactly one of ``abs`` or ``pct`` may be set. ``pct`` is a fraction, so
    ``0.05`` means "5% relative change is allowed". A metric with no tolerance
    block defaults to zero tolerance: any change at all is a breach.
    """

    model_config = ConfigDict(extra="forbid")

    abs: float | None = Field(
        default=None,
        description="Absolute change allowed, e.g. 100.0 means +/-100 is fine.",
    )
    pct: float | None = Field(
        default=None,
        description="Relative change allowed as a fraction, e.g. 0.05 = 5%.",
    )

    @field_validator("abs", "pct")
    @classmethod
    def _non_negative(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("tolerance values must be non-negative")
        return v

    @model_validator(mode="after")
    def _exactly_one(self) -> "Tolerance":
        if self.abs is not None and self.pct is not None:
            raise ValueError("set only one of tolerance.abs or tolerance.pct, not both")
        return self

    @property
    def is_zero(self) -> bool:
        return self.abs is None and self.pct is None

    def allowed_delta(self, baseline: float) -> float:
        """Return the maximum absolute change permitted from ``baseline``."""
        if self.abs is not None:
            return self.abs
        if self.pct is not None:
            return abs(baseline) * self.pct
        return 0.0

    def describe(self) -> str:
        if self.abs is not None:
            return f"+/-{self.abs:g} absolute"
        if self.pct is not None:
            return f"+/-{self.pct * 100:g}% relative"
        return "exact (no drift allowed)"


class Metric(BaseModel):
    """A single metric contract.

    The metric is computed as::

        SELECT <expression> FROM <source> WHERE <filters joined by AND>

    optionally grouped by ``dimensions`` to produce a per-segment breakdown in
    addition to the overall aggregate.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Stable identifier, used as the snapshot key.")
    description: str = Field(default="", description="Human-readable summary.")
    owner: str = Field(default="", description="Who is paged when this breaks.")
    source: str = Field(description="Table or view the metric reads from.")
    grain: str = Field(
        description="The row grain of the source, e.g. 'one row per order'. "
        "Documented and snapshotted so a grain change is visible in the diff.",
    )
    expression: str = Field(
        description="SQL aggregate expression producing the metric value, "
        "e.g. 'sum(amount)' or 'count(distinct user_id)'.",
    )
    filters: list[str] = Field(
        default_factory=list,
        description="SQL boolean predicates AND-ed together to scope the rows.",
    )
    dimensions: list[str] = Field(
        default_factory=list,
        description="Columns to break the metric down by for per-segment diffs.",
    )
    tolerance: Tolerance = Field(
        default_factory=Tolerance,
        description="Allowed drift before a diff counts as a regression.",
    )

    @field_validator("name")
    @classmethod
    def _name_is_identifier(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("metric name must not be empty")
        if any(c.isspace() for c in v):
            raise ValueError(f"metric name {v!r} must not contain whitespace")
        return v

    @field_validator("expression", "source", "grain")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field must not be blank")
        return v.strip()

    def definition_fingerprint(self) -> str:
        """Stable hash of everything that defines *how* the metric is computed.

        The value is intentionally independent of cosmetic fields like
        ``description`` and ``owner``: those can change without changing the
        number. A change to this fingerprint between snapshots is exactly the
        "someone quietly edited the join/filter" event we want to surface.
        """
        payload = {
            "source": self.source,
            "grain": self.grain,
            "expression": self.expression,
            "filters": sorted(self.filters),
            "dimensions": list(self.dimensions),
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class MetricSuite(BaseModel):
    """A named collection of metric contracts loaded from one YAML file."""

    model_config = ConfigDict(extra="forbid")

    suite: str = Field(default="default", description="Name of this suite.")
    database: str | None = Field(
        default=None,
        description="Path to the DuckDB database file the metrics read from. "
        "Relative paths are resolved against the contract file's directory.",
    )
    metrics: list[Metric] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> "MetricSuite":
        seen: set[str] = set()
        for m in self.metrics:
            if m.name in seen:
                raise ValueError(f"duplicate metric name {m.name!r} in suite")
            seen.add(m.name)
        return self

    def metric(self, name: str) -> Metric:
        for m in self.metrics:
            if m.name == name:
                return m
        raise KeyError(name)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MetricSuite":
        return cls.model_validate(data)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "MetricSuite":
        """Parse a suite from a YAML file, raising readable errors on bad input."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"contract file not found: {p}")
        raw = p.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:  # pragma: no cover - passthrough of lib error
            raise ValueError(f"{p} is not valid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{p} must contain a YAML mapping at the top level")
        suite = cls.model_validate(data)
        # Resolve a relative database path against the contract location.
        if suite.database and not Path(suite.database).is_absolute():
            suite.database = str((p.parent / suite.database).resolve())
        return suite
