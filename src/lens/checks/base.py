"""Base class for all LENS checks."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import polars as pl

from lens.types import CheckResult, Severity


class BaseCheck(ABC):
    """Base class that every LENS check must extend.

    To create a custom check:
        1. Subclass ``BaseCheck``
        2. Implement ``run()``
        3. Register with ``@registry.register`` or add to a YAML config
    """

    name: str = ""
    description: str = ""
    default_severity: Severity = Severity.WARNING

    def __init__(self, severity: Severity | None = None, **params: Any) -> None:
        self.severity = severity or self.default_severity
        self.params = params
        if not self.name:
            self.name = self.__class__.__name__

    @abstractmethod
    def run(
        self,
        data: pl.LazyFrame,
        *,
        entity_col: str = "entity_id",
        snapshot_col: str = "snapshot_date",
    ) -> CheckResult:
        """Execute the check and return a result."""
        ...
