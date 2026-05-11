"""Zero-shot time-series anomaly detection via TabPFN-TS.

Wraps the pretrained TabPFN time-series foundation model as a LENS check.
The model is pretrained once by the upstream authors on synthetic data —
no training on user data is required. Per-entity history is passed as
in-context examples; the model returns a predicted distribution for the
next step. We flag rows whose observed value falls more than
``score_threshold`` standard deviations from the predicted mean.

The TabPFN dependency is optional; install with ``pip install -e ".[tabpfn]"``.
For tests, inject a deterministic ``forecaster`` callable to avoid the
heavy import.
"""

from __future__ import annotations

from typing import Any, Callable

import polars as pl

from lens.checks.base import BaseCheck
from lens.checks.registry import registry
from lens.types import CheckResult, Issue, Severity

Forecaster = Callable[[list[float]], tuple[float, float]]
"""(history) -> (predicted_mean, predicted_std) for the next step."""


def _tabpfn_forecaster() -> Forecaster:
    """Resolve a TabPFN-TS-backed forecaster. Lazy-imported so the optional
    dependency is only required when actually used.
    """
    try:
        from tabpfn_time_series import TabPFNTimeSeriesRegressor
    except ImportError as e:
        raise ImportError(
            "TabPFNAnomalyCheck requires the 'tabpfn' extra. "
            'Install with: pip install -e ".[tabpfn]"'
        ) from e

    model = TabPFNTimeSeriesRegressor()

    def forecast(history: list[float]) -> tuple[float, float]:
        mean, std = model.predict_next(history)
        return float(mean), float(std)

    return forecast


@registry.register
class TabPFNAnomalyCheck(BaseCheck):
    """Flag entities whose latest snapshot deviates from a TabPFN-TS forecast.

    Parameters
    ----------
    field
        Name of the numeric column to monitor.
    context_window
        Number of prior snapshots per entity used as in-context history.
    score_threshold
        |z-score| above which a row is flagged. Default 3.0.
    min_history
        Skip entities with fewer than this many prior snapshots.
    forecaster
        Optional injected ``(history) -> (mean, std)`` callable. If ``None``,
        a TabPFN-TS-backed forecaster is lazily resolved. Tests inject a
        deterministic stub here.
    """

    name = "tabpfn_anomaly"
    description = "Zero-shot time-series anomaly detection via TabPFN-TS."
    default_severity = Severity.WARNING

    def __init__(
        self,
        field: str,
        context_window: int = 90,
        score_threshold: float = 3.0,
        min_history: int = 20,
        forecaster: Forecaster | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.field = field
        self.context_window = context_window
        self.score_threshold = score_threshold
        self.min_history = min_history
        self._forecaster = forecaster

    def _resolve_forecaster(self) -> Forecaster:
        if self._forecaster is None:
            self._forecaster = _tabpfn_forecaster()
        return self._forecaster

    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        df = data.sort(snapshot_col).select(entity_col, snapshot_col, self.field).collect()

        issues: list[Issue] = []
        forecast = None  # resolved lazily on first entity with enough history

        for entity, group in df.group_by(entity_col, maintain_order=True):
            entity_id = str(entity[0]) if isinstance(entity, tuple) else str(entity)
            values = group[self.field].to_list()
            if len(values) <= self.min_history:
                continue

            history = values[-(self.context_window + 1) : -1]
            observed = values[-1]
            if observed is None or any(v is None for v in history):
                continue

            if forecast is None:
                forecast = self._resolve_forecaster()

            mean, std = forecast([float(v) for v in history])
            if std <= 0:
                continue
            score = (float(observed) - mean) / std
            if abs(score) <= self.score_threshold:
                continue

            last_snapshot = group[snapshot_col].to_list()[-1]
            issues.append(
                Issue(
                    check_name=self.name,
                    severity=self.severity,
                    entity_id=entity_id,
                    field_name=self.field,
                    snapshot_date=last_snapshot,
                    description=(
                        f"TabPFN-TS anomaly: '{self.field}' observed={observed:.4f}, "
                        f"predicted_mean={mean:.4f}, predicted_std={std:.4f}, "
                        f"z_score={score:.2f}"
                    ),
                    details={
                        "observed": float(observed),
                        "predicted_mean": mean,
                        "predicted_std": std,
                        "score": score,
                        "context_window": len(history),
                    },
                )
            )

        return CheckResult(check_name=self.name, passed=len(issues) == 0, issues=issues)
