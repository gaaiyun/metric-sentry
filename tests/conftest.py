"""Shared fixtures: a fresh sample DuckDB database and a parsed sample suite."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from metric_sentry.contracts import MetricSuite
from metric_sentry.sample import SAMPLE_CONTRACT_YAML, build_sample_db


@pytest.fixture
def sample_db(tmp_path: Path) -> Path:
    db = tmp_path / "sample.duckdb"
    build_sample_db(db, rows=600, seed=7)
    return db


@pytest.fixture
def sample_suite(sample_db: Path) -> MetricSuite:
    data = yaml.safe_load(SAMPLE_CONTRACT_YAML)
    data["database"] = str(sample_db)
    return MetricSuite.from_dict(data)
