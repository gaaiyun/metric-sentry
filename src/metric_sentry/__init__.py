"""metric-sentry: treat your business metrics like code.

Snapshot and diff the *definition* and the *computed value* of every metric on
each change, so a silent edit to a join or filter that moves MRR by 12% gets
caught before it merges.
"""

__version__ = "0.1.0"

from metric_sentry.contracts import Metric, MetricSuite, Tolerance
from metric_sentry.executor import MetricResult, run_metric, run_suite
from metric_sentry.snapshot import Snapshot, load_snapshot, write_snapshot
from metric_sentry.diff import MetricDiff, SuiteDiff, diff_snapshots

__all__ = [
    "__version__",
    "Metric",
    "MetricSuite",
    "Tolerance",
    "MetricResult",
    "run_metric",
    "run_suite",
    "Snapshot",
    "load_snapshot",
    "write_snapshot",
    "MetricDiff",
    "SuiteDiff",
    "diff_snapshots",
]
