"""Tests for the diff engine and tolerance verdicts -- the core of the tool."""

from __future__ import annotations

import copy

import yaml

from metric_sentry.contracts import Metric, MetricSuite, Tolerance
from metric_sentry.executor import MetricResult, run_suite
from metric_sentry.diff import Verdict, diff_metric, diff_snapshots
from metric_sentry.sample import SAMPLE_CONTRACT_YAML
from metric_sentry.snapshot import Snapshot

FP = "samefingerprint00"


def _res(value, fingerprint=FP, breakdown=None):
    return MetricResult(
        name="m",
        value=value,
        row_count=10,
        breakdown=breakdown or {},
        fingerprint=fingerprint,
        grain="g",
    )


# --- single-metric tolerance logic -----------------------------------------


def test_no_change_is_ok():
    d = diff_metric("m", _res(100.0), _res(100.0), Tolerance())
    assert d.verdict == Verdict.OK
    assert d.abs_change == 0.0


def test_zero_tolerance_any_change_breaches():
    d = diff_metric("m", _res(100.0), _res(100.01), Tolerance())
    assert d.verdict == Verdict.BREACH


def test_change_within_abs_tolerance_ok():
    d = diff_metric("m", _res(100.0), _res(108.0), Tolerance(abs=10))
    assert d.verdict == Verdict.OK


def test_change_beyond_abs_tolerance_breaches():
    d = diff_metric("m", _res(100.0), _res(120.0), Tolerance(abs=10))
    assert d.verdict == Verdict.BREACH
    assert d.allowed_delta == 10
    assert d.abs_change == 20.0


def test_change_within_pct_tolerance_ok():
    d = diff_metric("m", _res(1000.0), _res(1030.0), Tolerance(pct=0.05))
    assert d.verdict == Verdict.OK
    assert d.pct_change == 0.03


def test_change_beyond_pct_tolerance_breaches():
    d = diff_metric("m", _res(1000.0), _res(1120.0), Tolerance(pct=0.05))
    assert d.verdict == Verdict.BREACH
    assert d.pct_change == 0.12


def test_pct_tolerance_boundary_is_inclusive():
    # exactly 5% on a 5% tolerance -> allowed.
    d = diff_metric("m", _res(1000.0), _res(1050.0), Tolerance(pct=0.05))
    assert d.verdict == Verdict.OK


def test_definition_change_overrides_tolerance():
    # Value barely moves, but fingerprint changed -> definition_changed, a breach.
    d = diff_metric(
        "m", _res(100.0, fingerprint="aaa"), _res(101.0, fingerprint="bbb"),
        Tolerance(pct=0.5),
    )
    assert d.verdict == Verdict.DEFINITION_CHANGED
    assert d.is_breach
    assert d.definition_changed


def test_new_metric():
    d = diff_metric("m", None, _res(100.0), Tolerance())
    assert d.verdict == Verdict.NEW
    assert not d.is_breach  # new metrics are reported, not blocked


def test_removed_metric():
    d = diff_metric("m", _res(100.0), None, Tolerance())
    assert d.verdict == Verdict.REMOVED
    assert not d.is_breach


def test_value_to_null_breaches():
    d = diff_metric("m", _res(100.0), _res(None), Tolerance(abs=1000))
    assert d.verdict == Verdict.BREACH
    assert "NULL" in d.note


def test_null_to_null_is_ok():
    d = diff_metric("m", _res(None), _res(None), Tolerance())
    assert d.verdict == Verdict.OK


def test_pct_change_against_zero_baseline_is_none():
    d = diff_metric("m", _res(0.0), _res(5.0), Tolerance(abs=10))
    assert d.pct_change is None
    assert d.verdict == Verdict.OK  # 5 <= abs tolerance 10


# --- dimension-level diff ---------------------------------------------------


def test_dimension_changes_only_report_moved_segments():
    base = _res(100.0, breakdown={"NA": 60.0, "EU": 40.0})
    cur = _res(110.0, breakdown={"NA": 70.0, "EU": 40.0})  # only NA moved
    d = diff_metric("m", base, cur, Tolerance(abs=20))
    keys = {dc.key for dc in d.dimension_changes}
    assert keys == {"NA"}
    assert d.dimension_changes[0].abs_change == 10.0


def test_dimension_appears_and_disappears():
    base = _res(100.0, breakdown={"NA": 100.0})
    cur = _res(100.0, breakdown={"EU": 100.0})
    d = diff_metric("m", base, cur, Tolerance(abs=0))
    keys = {dc.key for dc in d.dimension_changes}
    assert keys == {"NA", "EU"}


# --- whole-suite diff -------------------------------------------------------


def _sample_suite_with_db(db_path):
    data = yaml.safe_load(SAMPLE_CONTRACT_YAML)
    data["database"] = str(db_path)
    return MetricSuite.from_dict(data)


def test_clean_diff_has_no_breach(sample_db):
    suite = _sample_suite_with_db(sample_db)
    baseline = Snapshot.from_results(suite.suite, run_suite(suite))
    current = Snapshot.from_results(suite.suite, run_suite(suite))
    sd = diff_snapshots(baseline, current, suite=suite)
    assert not sd.has_breach
    assert all(d.verdict == Verdict.OK for d in sd)


def test_silent_filter_change_is_caught(sample_db):
    """The headline scenario: a quiet filter edit drops MRR and must be flagged."""
    base_data = yaml.safe_load(SAMPLE_CONTRACT_YAML)
    base_data["database"] = str(sample_db)
    suite = MetricSuite.from_dict(base_data)
    baseline = Snapshot.from_results(suite.suite, run_suite(suite))

    tampered = copy.deepcopy(base_data)
    tampered["metrics"][0]["filters"].append("plan != 'enterprise'")
    suite2 = MetricSuite.from_dict(tampered)
    current = Snapshot.from_results(suite2.suite, run_suite(suite2))

    sd = diff_snapshots(baseline, current, suite=suite2)
    assert sd.has_breach
    mrr = next(d for d in sd if d.name == "mrr")
    assert mrr.verdict == Verdict.DEFINITION_CHANGED
    assert mrr.current < mrr.baseline
    # Untouched metrics stay green.
    assert next(d for d in sd if d.name == "active_users").verdict == Verdict.OK


def test_suite_diff_serialisation(sample_db):
    suite = _sample_suite_with_db(sample_db)
    baseline = Snapshot.from_results(suite.suite, run_suite(suite))
    current = Snapshot.from_results(suite.suite, run_suite(suite))
    payload = diff_snapshots(baseline, current, suite=suite).as_dict()
    assert payload["suite"] == "saas_sample"
    assert payload["has_breach"] is False
    assert len(payload["metrics"]) == 3


def test_diff_without_suite_assumes_zero_tolerance():
    base = Snapshot.from_results("s", [_res(100.0)])
    cur = Snapshot.from_results("s", [_res(100.5)])
    sd = diff_snapshots(base, cur)  # no suite -> zero tolerance
    assert sd.has_breach
