"""Tests for contract parsing and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from metric_sentry.contracts import Metric, MetricSuite, Tolerance


def _minimal_metric(**overrides) -> dict:
    base = {
        "name": "mrr",
        "source": "orders",
        "grain": "one row per order",
        "expression": "sum(mrr)",
    }
    base.update(overrides)
    return base


def test_parse_minimal_metric():
    m = Metric.model_validate(_minimal_metric())
    assert m.name == "mrr"
    assert m.filters == []
    assert m.dimensions == []
    assert m.tolerance.is_zero


def test_parse_full_suite_from_dict():
    suite = MetricSuite.from_dict(
        {
            "suite": "s",
            "database": "x.duckdb",
            "metrics": [
                _minimal_metric(
                    filters=["status = 'active'"],
                    dimensions=["region"],
                    tolerance={"pct": 0.05},
                    owner="rev",
                ),
            ],
        }
    )
    m = suite.metric("mrr")
    assert m.owner == "rev"
    assert m.filters == ["status = 'active'"]
    assert m.dimensions == ["region"]
    assert m.tolerance.pct == 0.05


def test_blank_name_rejected():
    with pytest.raises(ValidationError):
        Metric.model_validate(_minimal_metric(name="  "))


def test_name_with_whitespace_rejected():
    with pytest.raises(ValidationError):
        Metric.model_validate(_minimal_metric(name="monthly revenue"))


def test_blank_expression_rejected():
    with pytest.raises(ValidationError):
        Metric.model_validate(_minimal_metric(expression="   "))


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        Metric.model_validate(_minimal_metric(foo="bar"))


def test_duplicate_metric_names_rejected():
    with pytest.raises(ValidationError):
        MetricSuite.from_dict(
            {
                "metrics": [_minimal_metric(), _minimal_metric()],
            }
        )


def test_empty_suite_rejected():
    with pytest.raises(ValidationError):
        MetricSuite.from_dict({"metrics": []})


def test_tolerance_both_set_rejected():
    with pytest.raises(ValidationError):
        Tolerance(abs=10, pct=0.1)


def test_tolerance_negative_rejected():
    with pytest.raises(ValidationError):
        Tolerance(pct=-0.1)


def test_tolerance_allowed_delta_abs():
    t = Tolerance(abs=10)
    assert t.allowed_delta(1000) == 10
    assert t.allowed_delta(-5) == 10


def test_tolerance_allowed_delta_pct():
    t = Tolerance(pct=0.05)
    assert t.allowed_delta(1000) == pytest.approx(50)
    assert t.allowed_delta(-200) == pytest.approx(10)


def test_tolerance_describe():
    assert Tolerance(abs=100).describe() == "+/-100 absolute"
    assert Tolerance(pct=0.05).describe() == "+/-5% relative"
    assert Tolerance().describe() == "exact (no drift allowed)"


def test_definition_fingerprint_stable():
    m1 = Metric.model_validate(_minimal_metric(filters=["a", "b"]))
    m2 = Metric.model_validate(_minimal_metric(filters=["b", "a"]))  # reordered
    # Filter order is normalised, so the fingerprint matches.
    assert m1.definition_fingerprint() == m2.definition_fingerprint()


def test_definition_fingerprint_ignores_cosmetic_fields():
    m1 = Metric.model_validate(_minimal_metric(owner="alice", description="x"))
    m2 = Metric.model_validate(_minimal_metric(owner="bob", description="y"))
    assert m1.definition_fingerprint() == m2.definition_fingerprint()


def test_definition_fingerprint_changes_on_filter():
    m1 = Metric.model_validate(_minimal_metric())
    m2 = Metric.model_validate(_minimal_metric(filters=["status = 'active'"]))
    assert m1.definition_fingerprint() != m2.definition_fingerprint()


def test_definition_fingerprint_changes_on_expression():
    m1 = Metric.model_validate(_minimal_metric(expression="sum(mrr)"))
    m2 = Metric.model_validate(_minimal_metric(expression="avg(mrr)"))
    assert m1.definition_fingerprint() != m2.definition_fingerprint()


def test_from_yaml_resolves_relative_db(tmp_path: Path):
    contract = tmp_path / "metrics.yml"
    contract.write_text(
        "suite: s\n"
        "database: data.duckdb\n"
        "metrics:\n"
        "  - name: m\n"
        "    source: t\n"
        "    grain: g\n"
        "    expression: count(*)\n",
        encoding="utf-8",
    )
    suite = MetricSuite.from_yaml(contract)
    # Relative DB path is resolved against the contract's directory.
    assert Path(suite.database) == (tmp_path / "data.duckdb").resolve()


def test_from_yaml_missing_file():
    with pytest.raises(FileNotFoundError):
        MetricSuite.from_yaml("does_not_exist.yml")


def test_from_yaml_non_mapping(tmp_path: Path):
    bad = tmp_path / "bad.yml"
    bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        MetricSuite.from_yaml(bad)


def test_sample_contract_parses():
    from metric_sentry.sample import SAMPLE_CONTRACT_YAML
    import yaml

    suite = MetricSuite.from_dict(yaml.safe_load(SAMPLE_CONTRACT_YAML))
    assert {m.name for m in suite.metrics} == {"mrr", "active_users", "conversion_rate"}
