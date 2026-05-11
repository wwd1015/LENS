"""STL-residual classical baseline TS detector.

Uses ``statsmodels.tsa.seasonal.STL`` (Seasonal-Trend decomposition using
LOESS, robust mode) to decompose each entity's time series into trend +
seasonal + residual components, then flags rows where the residual's
z-score exceeds ``z_threshold``. This is the "classical" baseline that
sits alongside the TabPFN-TS detector — fast, no model download, and a
sensible default when seasonality is present.

FALLBACK NOTE
-------------
If T13 eval shows score-averaged ensemble degrades recall, orchestrator
should pivot to detector-routing — pick one TS detector per field by data
shape (TabPFN-TS for smooth, STL for seasonal, rolling Z for sparse) —
rather than shipping a noisy ensemble. STL alone is fine as a baseline
when seasonality is present.

Guarded crash modes (per eng review item 7):
- Short series (``len(values) < min_history``) — entity is skipped, no Issue.
- Constant series (``residuals.std() == 0`` or NaN) — entity is skipped, no Issue.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import polars as pl
from statsmodels.tsa.seasonal import STL

from lens.checks.base import BaseCheck
from lens.checks.registry import registry
from lens.types import CheckResult, Issue, Severity

logger = logging.getLogger(__name__)


@registry.register
class STLResidualCheck(BaseCheck):
    """Flag rows where the STL-decomposition residual is an outlier.

    Parameters
    ----------
    field
        Name of the numeric column to monitor.
    period
        Seasonal period in snapshots. Default 30 (daily snapshots, ~monthly
        seasonality). Use ``period=12`` for monthly snapshots.
    z_threshold
        |z-score| of the residual above which a row is flagged. Default 3.0.
    min_history
        Skip entities with fewer than this many snapshots. Defaults to
        ``2 * period`` (STL needs at least two full seasonal cycles to
        decompose cleanly).
    """

    name = "stl_residual"
    description = "STL-decomposition residual z-score anomaly detector."
    default_severity = Severity.WARNING

    def __init__(
        self,
        field: str,
        period: int = 30,
        z_threshold: float = 3.0,
        min_history: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.field = field
        self.period = period
        self.z_threshold = z_threshold
        self.min_history = min_history

    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        min_history = self.min_history if self.min_history is not None else 2 * self.period

        df = data.sort(snapshot_col).select(entity_col, snapshot_col, self.field).collect()

        issues: list[Issue] = []

        for entity, group in df.group_by(entity_col, maintain_order=True):
            entity_value = entity[0] if isinstance(entity, tuple) else entity
            entity_id = str(entity_value)
            values_list = group[self.field].to_list()
            snapshots = group[snapshot_col].to_list()

            # Guard: short series
            if len(values_list) < min_history:
                logger.debug(
                    "stl_residual: skipping entity %s — len=%d < min_history=%d",
                    entity_id,
                    len(values_list),
                    min_history,
                )
                continue

            # Guard: missing values would break STL; skip entities with any null
            if any(v is None for v in values_list):
                logger.debug(
                    "stl_residual: skipping entity %s — contains null values", entity_id
                )
                continue

            values = np.asarray([float(v) for v in values_list], dtype=float)

            try:
                result = STL(values, period=self.period, robust=True).fit()
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(
                    "stl_residual: STL fit failed for entity %s: %s", entity_id, e
                )
                continue

            residuals = np.asarray(result.resid, dtype=float)
            std = float(np.nanstd(residuals))
            mean = float(np.nanmean(residuals))

            # Guard: constant / near-constant series produce zero-stdev residuals.
            # Also guard against NaN-only residuals. Use a relative-to-signal
            # floor so floating-point noise on a degenerate (essentially-constant)
            # series doesn't get amplified into spurious z-scores.
            signal_scale = float(np.nanmax(np.abs(values))) if values.size else 0.0
            degenerate_floor = max(1e-12, 1e-9 * signal_scale)
            if (
                not np.isfinite(std)
                or not np.isfinite(mean)
                or std <= degenerate_floor
            ):
                logger.debug(
                    "stl_residual: skipping entity %s — degenerate residuals (std=%r mean=%r)",
                    entity_id,
                    std,
                    mean,
                )
                continue

            z = (residuals - mean) / std
            flagged_idx = np.where(np.abs(z) > self.z_threshold)[0]

            for i in flagged_idx:
                z_value = float(z[i])
                confidence = min(1.0, max(0.0, abs(z_value) / 10.0))
                issues.append(
                    Issue(
                        check_name=self.name,
                        severity=self.severity,
                        entity_id=entity_id,
                        field_name=self.field,
                        snapshot_date=snapshots[int(i)],
                        description=(
                            f"STL residual z={z_value:.2f} exceeds threshold "
                            f"{self.z_threshold}"
                        ),
                        details={
                            "z_score": z_value,
                            "residual": float(residuals[int(i)]),
                            "field_value": float(values[int(i)]),
                        },
                        confidence=confidence,
                        detector_source="stl_residual",
                    )
                )

        return CheckResult(check_name=self.name, passed=len(issues) == 0, issues=issues)
