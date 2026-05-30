"""End-to-end CLI tests driving the real typer app via its test runner."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from metric_sentry.cli import app

runner = CliRunner()


def _init_project(tmp_path: Path) -> Path:
    result = runner.invoke(app, ["init", str(tmp_path), "--rows", "300"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "metrics.yml").exists()
    assert (tmp_path / "sample.duckdb").exists()
    return tmp_path


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("init", "run", "diff", "ci"):
        assert cmd in result.output


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "metric-sentry" in result.output


def test_init_run_diff_clean_flow(tmp_path: Path):
    project = _init_project(tmp_path)
    contract = project / "metrics.yml"
    snap = project / "golden.json"

    run_res = runner.invoke(
        app, ["run", "--contract", str(contract), "--out", str(snap)]
    )
    assert run_res.exit_code == 0, run_res.output
    assert snap.exists()
    assert "mrr" in run_res.output

    diff_res = runner.invoke(
        app, ["diff", "--contract", str(contract), "--baseline", str(snap)]
    )
    assert diff_res.exit_code == 0, diff_res.output
    assert "no breaches" in diff_res.output


def test_ci_passes_when_clean(tmp_path: Path):
    project = _init_project(tmp_path)
    contract = project / "metrics.yml"
    snap = project / "golden.json"
    runner.invoke(app, ["run", "--contract", str(contract), "--out", str(snap)])

    ci_res = runner.invoke(
        app, ["ci", "--contract", str(contract), "--baseline", str(snap)]
    )
    assert ci_res.exit_code == 0, ci_res.output


def test_ci_fails_on_silent_filter_change(tmp_path: Path):
    project = _init_project(tmp_path)
    contract = project / "metrics.yml"
    snap = project / "golden.json"
    runner.invoke(app, ["run", "--contract", str(contract), "--out", str(snap)])

    # Tamper: add a filter that silently drops revenue.
    data = yaml.safe_load(contract.read_text(encoding="utf-8"))
    data["metrics"][0]["filters"].append("plan != 'enterprise'")
    contract.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    ci_res = runner.invoke(
        app, ["ci", "--contract", str(contract), "--baseline", str(snap)]
    )
    assert ci_res.exit_code == 1, ci_res.output
    assert "mrr" in ci_res.output
    assert "breach" in ci_res.output.lower()


def test_diff_json_output(tmp_path: Path):
    project = _init_project(tmp_path)
    contract = project / "metrics.yml"
    snap = project / "golden.json"
    runner.invoke(app, ["run", "--contract", str(contract), "--out", str(snap)])

    diff_res = runner.invoke(
        app,
        ["diff", "--contract", str(contract), "--baseline", str(snap), "--json"],
    )
    assert diff_res.exit_code == 0, diff_res.output
    import json

    payload = json.loads(diff_res.output)
    assert payload["suite"] == "saas_sample"
    assert payload["has_breach"] is False


def test_run_missing_contract_exits_2(tmp_path: Path):
    res = runner.invoke(
        app, ["run", "--contract", str(tmp_path / "nope.yml")]
    )
    assert res.exit_code == 2


def test_diff_missing_baseline_exits_2(tmp_path: Path):
    project = _init_project(tmp_path)
    contract = project / "metrics.yml"
    res = runner.invoke(
        app,
        ["diff", "--contract", str(contract), "--baseline", str(tmp_path / "no.json")],
    )
    assert res.exit_code == 2


def test_init_refuses_overwrite_without_force(tmp_path: Path):
    _init_project(tmp_path)
    res = runner.invoke(app, ["init", str(tmp_path)])
    assert res.exit_code == 1
    assert "already exists" in res.output
