"""Base class for all LENS checks."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import polars as pl

from lens.types import CheckResult, Severity

logger = logging.getLogger(__name__)


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
        if params:
            # Every built-in check declares its real knobs as named
            # constructor args, so anything left over is almost certainly a
            # YAML typo (`z_treshold: 2.5`) — which would otherwise silently
            # run the check with its default tuning.
            logger.warning(
                "detector %r ignoring unknown parameter(s): %s — likely a "
                "typo in the config",
                self.name,
                sorted(params),
            )

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
