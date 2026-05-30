"""Tests for snapshot write/read round-trips."""

from __future__ import annotations

from pathlib import Path

import pytest

from metric_sentry.executor import MetricResult, run_suite
from metric_sentry.snapshot import (
    Snapshot,
    load_snapshot,
    write_breakdown_parquet,
    write_snapshot,
)


def _result(name="mrr", value=100.0):
    return MetricResult(
        name=name,
        value=value,
        row_count=5,
        breakdown={"NA": 60.0, "EU": 40.0},
        fingerprint="abc123",
        grain="one row per order",
    )


def test_snapshot_roundtrip(tmp_path: Path):
    snap = Snapshot.from_results("s", [_result()], meta={"contract": "metrics.yml"})
    path = tmp_path / "snap.json"
    write_snapshot(snap, path)

    loaded = load_snapshot(path)
    assert loaded.suite == "s"
    assert loaded.meta["contract"] == "metrics.yml"
    r = loaded.result("mrr")
    assert r.value == 100.0
    assert r.breakdown == {"NA": 60.0, "EU": 40.0}
    assert r.fingerprint == "abc123"
    assert r.grain == "one row per order"


def test_snapshot_roundtrip_preserves_null(tmp_path: Path):
    snap = Snapshot.from_results("s", [_result(value=None)])
    path = tmp_path / "snap.json"
    write_snapshot(snap, path)
    loaded = load_snapshot(path)
    assert loaded.result("mrr").value is None


def test_snapshot_creates_parent_dirs(tmp_path: Path):
    snap = Snapshot.from_results("s", [_result()])
    path = tmp_path / "nested" / "deep" / "snap.json"
    written = write_snapshot(snap, path)
    assert written.exists()


def test_load_missing_snapshot_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_snapshot(tmp_path / "nope.json")


def test_snapshot_metric_names(sample_suite):
    snap = Snapshot.from_results(sample_suite.suite, run_suite(sample_suite))
    assert snap.metric_names == ["mrr", "active_users", "conversion_rate"]


def test_result_lookup_missing():
    snap = Snapshot.from_results("s", [_result()])
    with pytest.raises(KeyError):
        snap.result("nope")


def test_parquet_breakdown_roundtrip(tmp_path: Path):
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    snap = Snapshot.from_results(
        "s",
        [
            _result(name="mrr", value=100.0),
            MetricResult(name="users", value=10.0, row_count=3, breakdown={}),
        ],
    )
    path = tmp_path / "bd.parquet"
    write_breakdown_parquet(snap, path)

    table = pq.read_table(path)
    df = table.to_pydict()
    # mrr has 2 dimension rows, users has 1 overall row -> 3 rows total.
    assert len(df["metric"]) == 3
    assert "__overall__" in df["dimension_key"]
