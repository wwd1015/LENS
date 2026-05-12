"""Top-down hierarchical drill-down detector.

Implements the "start at the portfolio, narrow into the segment that drove
the issue" workflow from the original LENS surveillance design.

Algorithm
---------
For a numeric ``field`` and an ordered list of segment columns
(``segments=[asset_class, vintage_quarter, ...]``):

1. **Compute aggregates at every depth**, from depth 0 (no segments,
   "portfolio") through depth ``min(max_depth, len(segments))``. At each
   depth, the data is grouped by ``(*segments[:depth], snapshot_col)`` and
   aggregated with ``agg`` (sum / mean / count / min / max).
2. **Score each (segment_combination, snapshot) against the combo's own
   history** via a z-score on the aggregated time series.
3. **Mark anomalous** every (path, snapshot) with ``|z| > z_threshold``,
   provided the slice has enough history (``min_history``) and enough
   distinct entities per snapshot (``min_segment_size``).
4. **Emit one Issue per "leaf"** — an anomalous (path, snapshot) is a leaf
   if no anomalous descendant exists on the same snapshot (i.e. no deeper
   segment-combination that extends this path with the same value
   constraints is also anomalous on that date). This produces the deepest
   still-anomalous path, which is the actual triage-valuable answer.

Note that anomalies are computed INDEPENDENTLY at every depth — drilling
isn't gated on parent state. A small sub-segment whose anomaly is too
small to move the parent aggregate is still surfaced; the design assumes
per-entity detectors run alongside drill-down for entity-level noise.

Guarded crash modes:
- Short series (``len < min_history``) → skip slice, no Issue.
- Tiny segments (``mean entities-per-snapshot < min_segment_size``) → skip.
- Constant series (``std == 0`` or NaN) → skip.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import polars as pl

from lens.checks.base import BaseCheck
from lens.checks.registry import registry
from lens.types import CheckResult, Issue, Severity

logger = logging.getLogger(__name__)


_AGG_MAP = {
    "sum": pl.col,
    "mean": pl.col,
    "count": pl.col,
    "min": pl.col,
    "max": pl.col,
}
"""Just to validate the agg name — actual expression built below."""


def _agg_expr(agg: str, field: str) -> pl.Expr:
    """Build the Polars aggregation expression for ``agg`` over ``field``."""
    col = pl.col(field)
    if agg == "sum":
        return col.sum()
    if agg == "mean":
        return col.mean()
    if agg == "count":
        return col.count()
    if agg == "min":
        return col.min()
    if agg == "max":
        return col.max()
    raise ValueError(
        f"unknown agg {agg!r}; expected one of sum/mean/count/min/max"
    )


@registry.register
class HierarchicalDrillDownCheck(BaseCheck):
    """Top-down drill-down anomaly detector — emits the deepest segment
    path whose aggregate deviates from its own history.
    """

    name = "hierarchical_drill_down"
    description = (
        "Compute the aggregate of a numeric field at every segment depth, "
        "z-score each time series, and emit the deepest still-anomalous "
        "(segment_path, snapshot) leaf — 'narrow down the scope of the issue.'"
    )
    default_severity = Severity.WARNING

    def __init__(
        self,
        field: str,
        segments: list[str] | None = None,
        agg: str = "sum",
        z_threshold: float = 3.0,
        max_depth: int | None = None,
        min_segment_size: int = 10,
        min_history: int = 14,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.field = field
        self.segments = list(segments or [])
        self.agg = agg
        # Validate the agg name now — fail fast at construction.
        _agg_expr(self.agg, self.field)
        self.z_threshold = float(z_threshold)
        self.max_depth = (
            int(max_depth) if max_depth is not None else len(self.segments)
        )
        self.min_segment_size = int(min_segment_size)
        self.min_history = int(min_history)

    # ------------------------------------------------------------------ run

    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        if isinstance(data, pl.LazyFrame):
            df = data.collect()
        elif isinstance(data, pl.DataFrame):
            df = data
        else:
            raise TypeError(f"expected polars frame, got {type(data).__name__}")

        max_depth = min(self.max_depth, len(self.segments))
        # depth -> list of (path_tuple, snapshot, z_score, value)
        # where path_tuple is a tuple of (col_name, value) pairs in order.
        anomalies_by_depth: dict[int, list[tuple]] = {}
        for depth in range(0, max_depth + 1):
            anomalies_by_depth[depth] = self._anomalies_at_depth(
                df, depth, entity_col, snapshot_col
            )

        # Filter to leaves: an anomaly (path, snap) is a leaf iff no deeper
        # anomaly on the SAME snapshot has a strictly-extending path.
        issues: list[Issue] = []
        for depth in range(0, max_depth + 1):
            for path, snap, z, value in anomalies_by_depth[depth]:
                if self._has_descendant(path, snap, anomalies_by_depth, depth):
                    continue
                issues.append(self._make_issue(path, snap, z, value))

        return CheckResult(
            check_name=self.name, passed=len(issues) == 0, issues=issues
        )

    # ------------------------------------------------------------- internals

    def _anomalies_at_depth(
        self,
        df: pl.DataFrame,
        depth: int,
        entity_col: str,
        snapshot_col: str,
    ) -> list[tuple]:
        """Group by ``segments[:depth]`` (or by nothing at depth 0); z-score
        each combination's aggregated time series; return anomalous rows.
        """
        seg_cols = self.segments[:depth]
        group_keys = seg_cols + [snapshot_col]

        try:
            agg_df = df.group_by(group_keys).agg(
                [
                    _agg_expr(self.agg, self.field).alias("__value__"),
                    pl.col(entity_col).n_unique().alias("__n__"),
                ]
            )
        except Exception as exc:  # noqa: BLE001 — best-effort per-depth
            logger.debug(
                "drill_down: aggregation failed at depth %d: %s", depth, exc
            )
            return []

        if depth == 0:
            sorted_df = agg_df.sort(snapshot_col)
            return self._z_score_series((), sorted_df, snapshot_col)

        # Iterate over unique segment-value combinations. Using
        # `partition_by` keeps each slice ordered with respect to the
        # original frame; we sort by snapshot_col before z-scoring.
        anomalies: list[tuple] = []
        try:
            partitions = agg_df.partition_by(seg_cols, as_dict=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "drill_down: partition_by failed at depth %d: %s", depth, exc
            )
            return []

        for combo_key, combo_df in partitions.items():
            # combo_key is either a single value or a tuple, depending on
            # whether seg_cols has one or many entries.
            if not isinstance(combo_key, tuple):
                combo_key = (combo_key,)
            path = tuple(zip(seg_cols, combo_key))
            sorted_df = combo_df.sort(snapshot_col)
            anomalies.extend(self._z_score_series(path, sorted_df, snapshot_col))
        return anomalies

    def _z_score_series(
        self,
        path: tuple,
        sorted_df: pl.DataFrame,
        snapshot_col: str,
    ) -> list[tuple]:
        """Compute z-scores on a single time series and return anomalies."""
        if sorted_df.height < self.min_history:
            return []

        # Guard: tiny segments — average entities per snapshot too small to
        # be statistically meaningful.
        n_per_snap = sorted_df["__n__"]
        if float(n_per_snap.mean() or 0) < self.min_segment_size:
            return []

        values = sorted_df["__value__"].to_numpy().astype(float)
        std = float(values.std())
        if std == 0.0 or np.isnan(std):
            return []
        mean = float(values.mean())
        z_scores = (values - mean) / std

        snapshots = sorted_df[snapshot_col].to_list()
        return [
            (path, snapshots[i], float(z_scores[i]), float(values[i]))
            for i in range(len(z_scores))
            if abs(z_scores[i]) > self.z_threshold
        ]

    def _has_descendant(
        self,
        path: tuple,
        snapshot: Any,
        anomalies_by_depth: dict[int, list[tuple]],
        current_depth: int,
    ) -> bool:
        """Is there an anomalous descendant of ``path`` on the same snapshot?

        Descendant = anomalous (other_path, snapshot) where other_path is
        deeper than path and every (col, val) pair in path also appears in
        other_path. Equivalent: other_path's depth > current_depth AND
        ``dict(path).items() <= dict(other_path).items()``.
        """
        path_items = set(path)
        max_depth = max(anomalies_by_depth.keys(), default=0)
        for deeper in range(current_depth + 1, max_depth + 1):
            for other_path, other_snap, _z, _v in anomalies_by_depth.get(
                deeper, []
            ):
                if other_snap != snapshot:
                    continue
                if path_items <= set(other_path):
                    return True
        return False

    def _make_issue(
        self, path: tuple, snapshot: Any, z_score: float, value: float
    ) -> Issue:
        path_label = (
            " > ".join(f"{c}={v}" for c, v in path) if path else "portfolio"
        )
        return Issue(
            check_name=self.name,
            severity=self.severity,
            entity_id=path_label,
            field_name=self.field,
            snapshot_date=snapshot,
            description=(
                f"{path_label} aggregate ({self.agg} {self.field}) "
                f"z={z_score:.2f} on {snapshot}"
            ),
            details={
                "segment_path": [
                    {"column": c, "value": str(v)} for c, v in path
                ],
                "depth": len(path),
                "agg": self.agg,
                "z_score": float(z_score),
                "value": float(value),
            },
            detector_source=self.name,
            confidence=min(1.0, abs(z_score) / 10.0),
        )
