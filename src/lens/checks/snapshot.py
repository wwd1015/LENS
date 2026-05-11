"""Built-in point-in-time snapshot checks (for completeness alongside temporal checks)."""

from __future__ import annotations

from typing import Any

import polars as pl

from lens.checks.base import BaseCheck
from lens.checks.registry import registry
from lens.types import CheckResult, Issue, Severity


@registry.register
class NullCheck(BaseCheck):
    """Flag entities with null/missing values in specified fields."""

    name = "null_check"
    description = "Flags rows where specified fields contain null values."
    default_severity = Severity.ERROR

    def __init__(self, fields: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fields = fields

    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        issues: list[Issue] = []
        for fld in self.fields:
            nulls = data.filter(pl.col(fld).is_null()).select(entity_col, snapshot_col).collect()
            for row in nulls.iter_rows(named=True):
                issues.append(
                    Issue(
                        check_name=self.name,
                        severity=self.severity,
                        entity_id=str(row[entity_col]),
                        field_name=fld,
                        snapshot_date=row[snapshot_col],
                        description=f"Null value in '{fld}'",
                    )
                )
        return CheckResult(check_name=self.name, passed=len(issues) == 0, issues=issues)


@registry.register
class RangeCheck(BaseCheck):
    """Flag numeric values outside an expected range."""

    name = "range_check"
    description = "Flags rows where a field falls outside [min_value, max_value]."
    default_severity = Severity.WARNING

    def __init__(
        self,
        field: str,
        min_value: float | None = None,
        max_value: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.field = field
        self.min_value = min_value
        self.max_value = max_value

    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        cond = pl.lit(False)
        if self.min_value is not None:
            cond = cond | (pl.col(self.field) < self.min_value)
        if self.max_value is not None:
            cond = cond | (pl.col(self.field) > self.max_value)

        violations = data.filter(cond).select(entity_col, snapshot_col, self.field).collect()

        issues = [
            Issue(
                check_name=self.name,
                severity=self.severity,
                entity_id=str(row[entity_col]),
                field_name=self.field,
                snapshot_date=row[snapshot_col],
                description=(
                    f"Value {row[self.field]} out of range "
                    f"[{self.min_value}, {self.max_value}]"
                ),
            )
            for row in violations.iter_rows(named=True)
        ]
        return CheckResult(check_name=self.name, passed=len(issues) == 0, issues=issues)
