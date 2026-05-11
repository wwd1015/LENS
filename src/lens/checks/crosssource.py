"""Built-in cross-source reconciliation checks."""

from __future__ import annotations

from typing import Any

import polars as pl

from lens.checks.base import BaseCheck
from lens.checks.registry import registry
from lens.types import CheckResult, Issue, Severity


@registry.register
class CrossSourceMatchCheck(BaseCheck):
    """Compare the same field across two data sources and flag mismatches."""

    name = "cross_source_match"
    description = "Flags entities where a field differs between two sources."
    default_severity = Severity.ERROR

    def __init__(
        self,
        field: str,
        tolerance: float = 0.0,
        tolerance_type: str = "absolute",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.field = field
        self.tolerance = tolerance
        if tolerance_type not in ("absolute", "relative"):
            raise ValueError("tolerance_type must be 'absolute' or 'relative'")
        self.tolerance_type = tolerance_type

    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        raise NotImplementedError(
            "CrossSourceMatchCheck requires two data sources. Use run_cross() instead."
        )

    def run_cross(
        self,
        source_a: pl.LazyFrame,
        source_b: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
        source_a_name: str = "source_a",
        source_b_name: str = "source_b",
    ) -> CheckResult:
        """Run the cross-source comparison.

        Both frames must share ``entity_col`` and ``snapshot_col`` as join keys.
        """
        field_a = f"{self.field}_{source_a_name}"
        field_b = f"{self.field}_{source_b_name}"

        joined = (
            source_a.select(entity_col, snapshot_col, pl.col(self.field).alias(field_a))
            .join(
                source_b.select(entity_col, snapshot_col, pl.col(self.field).alias(field_b)),
                on=[entity_col, snapshot_col],
                how="inner",
            )
        )

        diff_col = "__diff"
        if self.tolerance_type == "absolute":
            joined = joined.with_columns(
                (pl.col(field_a).cast(pl.Float64) - pl.col(field_b).cast(pl.Float64))
                .abs()
                .alias(diff_col)
            )
        else:
            joined = joined.with_columns(
                (
                    (pl.col(field_a).cast(pl.Float64) - pl.col(field_b).cast(pl.Float64))
                    / pl.col(field_a).cast(pl.Float64)
                )
                .abs()
                .alias(diff_col)
            )

        mismatches = joined.filter(pl.col(diff_col) > self.tolerance).collect()

        issues = [
            Issue(
                check_name=self.name,
                severity=self.severity,
                entity_id=str(row[entity_col]),
                field_name=self.field,
                snapshot_date=row[snapshot_col],
                description=(
                    f"Mismatch on '{self.field}': "
                    f"{source_a_name}={row[field_a]}, {source_b_name}={row[field_b]} "
                    f"(diff={row[diff_col]:.4f})"
                ),
            )
            for row in mismatches.iter_rows(named=True)
        ]
        return CheckResult(check_name=self.name, passed=len(issues) == 0, issues=issues)
