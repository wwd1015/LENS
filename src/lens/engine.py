"""Suite engine — orchestrates running multiple checks."""

from __future__ import annotations

from typing import Any

import polars as pl

from lens.checks.base import BaseCheck
from lens.checks.registry import registry
from lens.io.base import DataSource
from lens.types import SuiteResult


class Suite:
    """A collection of checks to run against one or more data sources.

    Example::

        suite = Suite(entity_col="loan_id", snapshot_col="as_of_date")
        suite.add("null_check", fields=["balance", "status"])
        suite.add("monotonicity", field="cumulative_payments", direction="increasing")
        result = suite.run(source)
    """

    def __init__(
        self,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> None:
        self.entity_col = entity_col
        self.snapshot_col = snapshot_col
        self._checks: list[BaseCheck] = []

    def add(self, check: str | BaseCheck, **kwargs: Any) -> Suite:
        """Add a check by name (from registry) or by instance."""
        if isinstance(check, str):
            self._checks.append(registry.create(check, **kwargs))
        else:
            self._checks.append(check)
        return self

    def run(self, source: DataSource | pl.LazyFrame | pl.DataFrame) -> SuiteResult:
        """Run all checks against the given data source."""
        if isinstance(source, DataSource):
            data = source.read()
        elif isinstance(source, pl.DataFrame):
            data = source.lazy()
        else:
            data = source

        result = SuiteResult()
        for check in self._checks:
            check_result = check.run(
                data,
                entity_col=self.entity_col,
                snapshot_col=self.snapshot_col,
            )
            result.results.append(check_result)
        return result
