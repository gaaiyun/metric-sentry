"""Golden snapshots.

A snapshot is the frozen, committed-to-git record of every metric's definition
fingerprint and computed value at a known-good point in time. Diffing the
current run against the golden snapshot is what catches regressions.

Snapshots are stored as JSON because JSON is line-diffable in code review: a
reviewer can see the metric value change right next to the contract change in
the same PR. A Parquet writer is also provided for teams that prefer columnar
storage / large dimension breakdowns; it round-trips the same data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from metric_sentry import __version__
from metric_sentry.executor import MetricResult

SCHEMA_VERSION = 1


@dataclass
class Snapshot:
    """An immutable record of a suite's metric values at one moment."""

    suite: str
    created_at: str
    results: list[MetricResult]
    schema_version: int = SCHEMA_VERSION
    tool_version: str = __version__
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_results(
        cls,
        suite: str,
        results: list[MetricResult],
        meta: dict[str, Any] | None = None,
    ) -> "Snapshot":
        return cls(
            suite=suite,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            results=list(results),
            meta=meta or {},
        )

    def result(self, name: str) -> MetricResult:
        for r in self.results:
            if r.name == name:
                return r
        raise KeyError(name)

    @property
    def metric_names(self) -> list[str]:
        return [r.name for r in self.results]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tool_version": self.tool_version,
            "suite": self.suite,
            "created_at": self.created_at,
            "meta": self.meta,
            "metrics": [r.as_dict() for r in self.results],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        results = [
            MetricResult(
                name=m["name"],
                value=m["value"],
                row_count=m["row_count"],
                breakdown=m.get("breakdown", {}),
                fingerprint=m.get("fingerprint", ""),
                grain=m.get("grain", ""),
            )
            for m in data["metrics"]
        ]
        return cls(
            suite=data["suite"],
            created_at=data["created_at"],
            results=results,
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            tool_version=data.get("tool_version", "unknown"),
            meta=data.get("meta", {}),
        )


def write_snapshot(snapshot: Snapshot, path: str | Path) -> Path:
    """Write ``snapshot`` to ``path`` as pretty JSON. Returns the resolved path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False, sort_keys=False)
    p.write_text(payload + "\n", encoding="utf-8")
    return p


def load_snapshot(path: str | Path) -> Snapshot:
    """Load a snapshot previously written by :func:`write_snapshot`."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"snapshot not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    return Snapshot.from_dict(data)


def write_breakdown_parquet(snapshot: Snapshot, path: str | Path) -> Path:
    """Write the per-dimension breakdowns to a Parquet file (optional format).

    One row per (metric, dimension_key, value). Requires pyarrow. This is a
    convenience for teams standardising on Parquet; the JSON snapshot remains
    the canonical, diff-reviewable artifact.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    metrics: list[str] = []
    dim_keys: list[str] = []
    values: list[float | None] = []
    for r in snapshot.results:
        if not r.breakdown:
            metrics.append(r.name)
            dim_keys.append("__overall__")
            values.append(r.value)
            continue
        for key, val in r.breakdown.items():
            metrics.append(r.name)
            dim_keys.append(key)
            values.append(val)

    table = pa.table(
        {
            "metric": pa.array(metrics, type=pa.string()),
            "dimension_key": pa.array(dim_keys, type=pa.string()),
            "value": pa.array(values, type=pa.float64()),
        }
    )
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, p)
    return p
