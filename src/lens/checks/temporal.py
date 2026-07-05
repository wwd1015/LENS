"""Built-in temporal / longitudinal checks."""

from __future__ import annotations

from typing import Any

import polars as pl

from lens.checks.base import BaseCheck
from lens.checks.registry import registry
from lens.types import CheckResult, Issue, Severity


@registry.register
class StaleDataCheck(BaseCheck):
    """Detect fields that haven't changed for an unusually long period."""

    name = "stale_data"
    description = "Flags entities where a field has not changed for more than N snapshots."
    default_severity = Severity.WARNING

    def __init__(self, field: str, max_unchanged: int = 30, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.field = field
        self.max_unchanged = max_unchanged

    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        # The canonical stale-feed failure is TRAILING staleness: an entity
        # that updated normally and then froze. Per entity (sorted by
        # snapshot), count the run of trailing snapshots equal to the latest
        # value — matching value-vs-last, reversing, and cum_min-ing the
        # boolean gives 1s for the trailing unchanged run and 0s after the
        # first change. null == null counts as unchanged (a frozen null feed
        # is still frozen).
        value = pl.col(self.field)
        unchanged_vs_last = (value == value.last()) | (value.is_null() & value.last().is_null())
        trailing_run = unchanged_vs_last.cast(pl.Int32).reverse().cum_min().sum()

        df = (
            data.sort(snapshot_col)
            .group_by(entity_col)
            .agg(
                trailing_run.alias("trailing_unchanged"),
                pl.col(snapshot_col).last().alias("last_snapshot"),
            )
            .filter(pl.col("trailing_unchanged") > self.max_unchanged)
            .collect()
        )

        issues = [
            Issue(
                check_name=self.name,
                severity=self.severity,
                entity_id=str(row[entity_col]),
                field_name=self.field,
                snapshot_date=row["last_snapshot"],
                description=(
                    f"Field '{self.field}' unchanged across the last "
                    f"{row['trailing_unchanged']} snapshots"
                ),
                details={"trailing_unchanged": row["trailing_unchanged"]},
            )
            for row in df.iter_rows(named=True)
        ]
        return CheckResult(check_name=self.name, passed=len(issues) == 0, issues=issues)


@registry.register
class MonotonicityCheck(BaseCheck):
    """Verify a field is monotonically increasing or decreasing over time."""

    name = "monotonicity"
    description = "Flags entities where a field violates expected monotonic trend."
    default_severity = Severity.ERROR

    def __init__(self, field: str, direction: str = "increasing", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.field = field
        if direction not in ("increasing", "decreasing"):
            raise ValueError(f"direction must be 'increasing' or 'decreasing', got '{direction}'")
        self.direction = direction

    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        diff_col = f"__{self.field}_diff"
        df = (
            data.sort(snapshot_col)
            .with_columns(
                (pl.col(self.field) - pl.col(self.field).shift(1))
                .over(entity_col)
                .alias(diff_col)
            )
        )

        if self.direction == "increasing":
            violations = df.filter(pl.col(diff_col) < 0)
        else:
            violations = df.filter(pl.col(diff_col) > 0)

        vdf = violations.select(entity_col, snapshot_col, self.field, diff_col).collect()

        issues = [
            Issue(
                check_name=self.name,
                severity=self.severity,
                entity_id=str(row[entity_col]),
                field_name=self.field,
                snapshot_date=row[snapshot_col],
                description=(
                    f"Monotonicity violation: '{self.field}' should be {self.direction}, "
                    f"but changed by {row[diff_col]}"
                ),
            )
            for row in vdf.iter_rows(named=True)
        ]
        return CheckResult(check_name=self.name, passed=len(issues) == 0, issues=issues)


@registry.register
class VolatilityCheck(BaseCheck):
    """Detect abnormal period-over-period changes in a numeric field."""

    name = "volatility"
    description = "Flags entities where a field changes by more than a threshold between snapshots."
    default_severity = Severity.WARNING

    def __init__(
        self, field: str, max_pct_change: float = 0.5, absolute: bool = False, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.field = field
        self.max_pct_change = max_pct_change
        self.absolute = absolute

    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        prev_col = f"__{self.field}_prev"
        change_col = f"__{self.field}_change"

        df = data.sort(snapshot_col).with_columns(
            pl.col(self.field).shift(1).over(entity_col).alias(prev_col)
        )

        if self.absolute:
            df = df.with_columns(
                (pl.col(self.field) - pl.col(prev_col)).abs().alias(change_col)
            )
            violations = df.filter(pl.col(change_col) > self.max_pct_change)
        else:
            df = df.with_columns(
                ((pl.col(self.field) - pl.col(prev_col)) / pl.col(prev_col))
                .abs()
                .alias(change_col)
            )
            # prev == 0 makes the relative change infinite — a field that
            # legitimately starts at zero (new originations, drawdowns) would
            # spam "changed by inf%" findings. Skip those transitions.
            violations = df.filter(
                (pl.col(change_col) > self.max_pct_change) & (pl.col(prev_col) != 0)
            )

        vdf = violations.select(entity_col, snapshot_col, self.field, change_col).collect()

        issues = [
            Issue(
                check_name=self.name,
                severity=self.severity,
                entity_id=str(row[entity_col]),
                field_name=self.field,
                snapshot_date=row[snapshot_col],
                description=(
                    f"Volatility spike: '{self.field}' changed by "
                    + (
                        f"${row[change_col]:.4f}"
                        if self.absolute
                        else f"{row[change_col] * 100.0:.2f}%"
                    )
                ),
            )
            for row in vdf.iter_rows(named=True)
        ]
        return CheckResult(check_name=self.name, passed=len(issues) == 0, issues=issues)
