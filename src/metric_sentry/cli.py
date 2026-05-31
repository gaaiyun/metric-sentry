"""metric-sentry command-line interface.

Subcommands
-----------
init   Scaffold a project: write a sample contract + build the sample DuckDB DB.
run    Compute every metric in a contract and write a golden snapshot.
diff   Recompute metrics and diff them against a golden snapshot (report only).
ci     Same as diff, but exit non-zero on a breach so a PR gate can block.

The CLI is deliberately thin: it parses args, calls the library, and renders.
All the real logic lives in the importable package so it is unit-testable.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from metric_sentry import __version__
from metric_sentry.contracts import MetricSuite
from metric_sentry.diff import MetricDiff, SuiteDiff, Verdict, diff_snapshots
from metric_sentry.executor import run_suite
from metric_sentry.sample import build_sample_db, write_sample_contract
from metric_sentry.snapshot import Snapshot, load_snapshot, write_snapshot

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Treat your business metrics like code: snapshot and diff them on every PR.",
)

DEFAULT_CONTRACT = "metrics.yml"
DEFAULT_SNAPSHOT = "snapshots/golden.json"


def _err(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)


def _ok(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.GREEN)


def _load_suite(contract: Path) -> MetricSuite:
    try:
        return MetricSuite.from_yaml(contract)
    except FileNotFoundError as exc:
        _err(str(exc))
        raise typer.Exit(code=2)
    except ValueError as exc:
        # pydantic's ValidationError subclasses ValueError, so this also covers
        # a malformed contract. typer.Exit is *not* caught here (it is not a
        # ValueError), so control-flow exits raised above propagate correctly.
        _err(f"failed to parse {contract}: {exc}")
        raise typer.Exit(code=2)


def _fmt(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        # Trim noise but keep meaningful precision.
        return f"{value:,.4f}".rstrip("0").rstrip(".") if value % 1 else f"{value:,.0f}"
    return str(value)


@app.command()
def version() -> None:
    """Print the metric-sentry version."""
    typer.echo(f"metric-sentry {__version__}")


@app.command()
def init(
    directory: Path = typer.Argument(
        Path("."),
        help="Directory to scaffold the sample project into.",
    ),
    rows: int = typer.Option(600, help="Number of sample order rows to generate."),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing contract file."
    ),
) -> None:
    """Scaffold a runnable sample project (contract + DuckDB sample data)."""
    directory.mkdir(parents=True, exist_ok=True)
    contract_path = directory / DEFAULT_CONTRACT

    if contract_path.exists() and not force:
        _err(f"{contract_path} already exists; pass --force to overwrite.")
        raise typer.Exit(code=1)

    write_sample_contract(contract_path)
    db_path = directory / "sample.duckdb"
    build_sample_db(db_path, rows=rows)

    _ok(f"Initialised metric-sentry project in {directory.resolve()}")
    typer.echo(f"  contract : {contract_path}")
    typer.echo(f"  sample DB: {db_path}  ({rows} orders)")
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo(f"  metric-sentry run  --contract {contract_path} --out {DEFAULT_SNAPSHOT}")
    typer.echo(f"  metric-sentry diff --contract {contract_path} --baseline {DEFAULT_SNAPSHOT}")


@app.command()
def run(
    contract: Path = typer.Option(
        Path(DEFAULT_CONTRACT), "--contract", "-c", help="Path to the contract YAML."
    ),
    out: Path = typer.Option(
        Path(DEFAULT_SNAPSHOT), "--out", "-o", help="Where to write the snapshot JSON."
    ),
    show: bool = typer.Option(True, help="Print the computed values."),
) -> None:
    """Compute every metric in the contract and write a golden snapshot."""
    suite = _load_suite(contract)
    results = run_suite(suite)
    snapshot = Snapshot.from_results(
        suite.suite, results, meta={"contract": str(contract)}
    )
    written = write_snapshot(snapshot, out)

    if show:
        typer.echo(f"suite: {suite.suite}   ({len(results)} metrics)")
        for r in results:
            owner = suite.metric(r.name).owner or "-"
            typer.echo(
                f"  {r.name:18s} = {_fmt(r.value):>16}   "
                f"[rows={r.row_count}, owner={owner}]"
            )
    _ok(f"wrote snapshot -> {written}")


def _verdict_color(verdict: Verdict) -> str:
    return {
        Verdict.OK: typer.colors.GREEN,
        Verdict.BREACH: typer.colors.RED,
        Verdict.DEFINITION_CHANGED: typer.colors.RED,
        Verdict.NEW: typer.colors.CYAN,
        Verdict.REMOVED: typer.colors.YELLOW,
    }.get(verdict, typer.colors.WHITE)


def _render_diff(sd: SuiteDiff, show_dimensions: bool = True) -> None:
    typer.echo(f"suite: {sd.suite}")
    for d in sd.diffs:
        marker = {
            Verdict.OK: "OK ",
            Verdict.BREACH: "!! ",
            Verdict.DEFINITION_CHANGED: "## ",
            Verdict.NEW: "++ ",
            Verdict.REMOVED: "-- ",
        }.get(d.verdict, "?  ")
        pct = f"{d.pct_change * 100:+.2f}%" if d.pct_change is not None else "n/a"
        line = (
            f"{marker}{d.name:18s} "
            f"{_fmt(d.baseline):>14} -> {_fmt(d.current):<14} "
            f"({pct})  [{d.verdict.value}]"
        )
        typer.secho(line, fg=_verdict_color(d.verdict))
        if d.note:
            typer.echo(f"      {d.note}")
        if show_dimensions and d.dimension_changes:
            for dc in d.dimension_changes:
                typer.echo(
                    f"      - {dc.key:12s} {_fmt(dc.baseline):>12} -> "
                    f"{_fmt(dc.current):<12} ({_fmt(dc.abs_change)})"
                )

    if sd.has_breach:
        typer.secho(
            f"\n{len(sd.breaches)} breach(es): "
            + ", ".join(d.name for d in sd.breaches),
            fg=typer.colors.RED,
            bold=True,
        )
    else:
        _ok("\nno breaches: all metrics within tolerance")


def _compute_diff(contract: Path, baseline: Path) -> SuiteDiff:
    suite = _load_suite(contract)
    try:
        base_snap = load_snapshot(baseline)
    except FileNotFoundError as exc:
        _err(str(exc))
        _err("run `metric-sentry run` first to create a golden snapshot.")
        raise typer.Exit(code=2)
    current_snap = Snapshot.from_results(suite.suite, run_suite(suite))
    return diff_snapshots(base_snap, current_snap, suite=suite)


@app.command()
def diff(
    contract: Path = typer.Option(
        Path(DEFAULT_CONTRACT), "--contract", "-c", help="Path to the contract YAML."
    ),
    baseline: Path = typer.Option(
        Path(DEFAULT_SNAPSHOT),
        "--baseline",
        "-b",
        help="Golden snapshot to diff the current run against.",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the diff as JSON instead of a table."
    ),
    no_dimensions: bool = typer.Option(
        False, "--no-dimensions", help="Hide the per-dimension breakdown."
    ),
) -> None:
    """Recompute metrics and diff against the golden snapshot (report only)."""
    sd = _compute_diff(contract, baseline)
    if json_out:
        typer.echo(json.dumps(sd.as_dict(), indent=2, ensure_ascii=False))
    else:
        _render_diff(sd, show_dimensions=not no_dimensions)


@app.command()
def ci(
    contract: Path = typer.Option(
        Path(DEFAULT_CONTRACT), "--contract", "-c", help="Path to the contract YAML."
    ),
    baseline: Path = typer.Option(
        Path(DEFAULT_SNAPSHOT),
        "--baseline",
        "-b",
        help="Golden snapshot to gate against.",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the diff as JSON instead of a table."
    ),
) -> None:
    """Gate a PR: diff against golden and exit non-zero if any metric breaches.

    Intended to run in CI. Definition changes and out-of-tolerance value moves
    both fail the build; ``new``/``removed`` metrics are reported but do not.
    """
    sd = _compute_diff(contract, baseline)
    if json_out:
        typer.echo(json.dumps(sd.as_dict(), indent=2, ensure_ascii=False))
    else:
        _render_diff(sd)

    if sd.has_breach:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
