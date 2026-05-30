"""Value-level diff with tolerance verdicts.

This is the part that earns the "sentry" in the name. Given a baseline snapshot
(the golden, committed values) and a current snapshot (what the metrics compute
*now*, on this PR), it reports for every metric:

* whether the **definition** changed (fingerprint moved -> someone edited the
  join / filter / expression / grain),
* the **absolute and relative change** in the headline value,
* the **per-dimension** changes, so you can see *which* segment moved, and
* a **verdict** -- ``ok`` / ``breach`` / ``definition_changed`` / ``new`` /
  ``removed`` -- by comparing the change against the metric's tolerance.

A breach is what a CI gate blocks on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from metric_sentry.contracts import MetricSuite, Tolerance
from metric_sentry.snapshot import Snapshot


class Verdict(str, Enum):
    OK = "ok"
    BREACH = "breach"
    DEFINITION_CHANGED = "definition_changed"
    NEW = "new"
    REMOVED = "removed"


@dataclass
class DimensionChange:
    key: str
    baseline: float | None
    current: float | None
    abs_change: float | None

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "baseline": self.baseline,
            "current": self.current,
            "abs_change": self.abs_change,
        }


@dataclass
class MetricDiff:
    """Diff of one metric between baseline and current snapshots."""

    name: str
    verdict: Verdict
    baseline: float | None
    current: float | None
    abs_change: float | None
    pct_change: float | None
    tolerance: str
    allowed_delta: float | None
    definition_changed: bool
    baseline_fingerprint: str
    current_fingerprint: str
    dimension_changes: list[DimensionChange] = field(default_factory=list)
    note: str = ""

    @property
    def is_breach(self) -> bool:
        return self.verdict in (Verdict.BREACH, Verdict.DEFINITION_CHANGED)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "verdict": self.verdict.value,
            "baseline": self.baseline,
            "current": self.current,
            "abs_change": self.abs_change,
            "pct_change": self.pct_change,
            "tolerance": self.tolerance,
            "allowed_delta": self.allowed_delta,
            "definition_changed": self.definition_changed,
            "baseline_fingerprint": self.baseline_fingerprint,
            "current_fingerprint": self.current_fingerprint,
            "dimension_changes": [d.as_dict() for d in self.dimension_changes],
            "note": self.note,
        }


@dataclass
class SuiteDiff:
    """Diff of a whole suite. Iterable over its per-metric diffs."""

    suite: str
    diffs: list[MetricDiff]

    def __iter__(self):
        return iter(self.diffs)

    @property
    def breaches(self) -> list[MetricDiff]:
        return [d for d in self.diffs if d.is_breach]

    @property
    def has_breach(self) -> bool:
        return bool(self.breaches)

    def as_dict(self) -> dict:
        return {
            "suite": self.suite,
            "has_breach": self.has_breach,
            "breach_count": len(self.breaches),
            "metrics": [d.as_dict() for d in self.diffs],
        }


def _pct_change(baseline: float | None, current: float | None) -> float | None:
    if baseline is None or current is None:
        return None
    if baseline == 0:
        return None  # relative change undefined against a zero baseline
    return (current - baseline) / abs(baseline)


def _abs_change(baseline: float | None, current: float | None) -> float | None:
    if baseline is None or current is None:
        return None
    return current - baseline


def _dimension_changes(
    baseline_bd: dict[str, float | None],
    current_bd: dict[str, float | None],
) -> list[DimensionChange]:
    keys = sorted(set(baseline_bd) | set(current_bd))
    out: list[DimensionChange] = []
    for k in keys:
        b = baseline_bd.get(k)
        c = current_bd.get(k)
        change = _abs_change(b, c)
        # Only surface dimensions that actually moved (or appeared/vanished).
        if b == c:
            continue
        out.append(DimensionChange(key=k, baseline=b, current=c, abs_change=change))
    return out


def diff_metric(
    name: str,
    baseline_result,
    current_result,
    tolerance: Tolerance,
) -> MetricDiff:
    """Diff a single metric. Either result may be ``None`` (new / removed)."""
    if baseline_result is None and current_result is not None:
        return MetricDiff(
            name=name,
            verdict=Verdict.NEW,
            baseline=None,
            current=current_result.value,
            abs_change=None,
            pct_change=None,
            tolerance=tolerance.describe(),
            allowed_delta=None,
            definition_changed=False,
            baseline_fingerprint="",
            current_fingerprint=current_result.fingerprint,
            note="metric not present in baseline snapshot",
        )
    if current_result is None and baseline_result is not None:
        return MetricDiff(
            name=name,
            verdict=Verdict.REMOVED,
            baseline=baseline_result.value,
            current=None,
            abs_change=None,
            pct_change=None,
            tolerance=tolerance.describe(),
            allowed_delta=None,
            definition_changed=False,
            baseline_fingerprint=baseline_result.fingerprint,
            current_fingerprint="",
            note="metric no longer defined in current run",
        )

    baseline = baseline_result.value
    current = current_result.value
    abs_change = _abs_change(baseline, current)
    pct_change = _pct_change(baseline, current)
    allowed = tolerance.allowed_delta(baseline) if baseline is not None else None

    definition_changed = (
        baseline_result.fingerprint != current_result.fingerprint
        and bool(baseline_result.fingerprint)
        and bool(current_result.fingerprint)
    )

    dim_changes = _dimension_changes(
        baseline_result.breakdown, current_result.breakdown
    )

    # Decide the verdict.
    note = ""
    if definition_changed:
        verdict = Verdict.DEFINITION_CHANGED
        note = (
            "metric definition changed (join/filter/expression/grain). "
            "Review the contract diff: the number is no longer comparable."
        )
    elif abs_change is None:
        # One side is NULL -> cannot evaluate tolerance numerically.
        if baseline == current:
            verdict = Verdict.OK
        else:
            verdict = Verdict.BREACH
            note = "value moved to/from NULL"
    elif abs(abs_change) <= allowed + 1e-12:
        verdict = Verdict.OK
    else:
        verdict = Verdict.BREACH
        note = (
            f"change {abs_change:+.4g} exceeds allowed {allowed:.4g} "
            f"({tolerance.describe()})"
        )

    return MetricDiff(
        name=name,
        verdict=verdict,
        baseline=baseline,
        current=current,
        abs_change=abs_change,
        pct_change=pct_change,
        tolerance=tolerance.describe(),
        allowed_delta=allowed,
        definition_changed=definition_changed,
        baseline_fingerprint=baseline_result.fingerprint,
        current_fingerprint=current_result.fingerprint,
        dimension_changes=dim_changes,
        note=note,
    )


def diff_snapshots(
    baseline: Snapshot,
    current: Snapshot,
    suite: MetricSuite | None = None,
) -> SuiteDiff:
    """Diff two snapshots, applying each metric's tolerance from ``suite``.

    If ``suite`` is omitted, zero tolerance is assumed for every metric (any
    change is a breach). Passing the suite is how per-metric tolerances from the
    contract are honoured.
    """
    tolerances: dict[str, Tolerance] = {}
    if suite is not None:
        tolerances = {m.name: m.tolerance for m in suite.metrics}

    names = list(
        dict.fromkeys([*baseline.metric_names, *current.metric_names])
    )  # preserve order, dedupe

    diffs: list[MetricDiff] = []
    for name in names:
        b = _safe_result(baseline, name)
        c = _safe_result(current, name)
        tol = tolerances.get(name, Tolerance())
        diffs.append(diff_metric(name, b, c, tol))

    return SuiteDiff(suite=current.suite, diffs=diffs)


def _safe_result(snapshot: Snapshot, name: str):
    try:
        return snapshot.result(name)
    except KeyError:
        return None
