"""Abstract base class for data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod

import polars as pl


class DataSource(ABC):
    """Base class for all LENS data sources.

    Subclass this to add new connectors (Snowflake, Parquet, Delta, etc.).
    """

    @abstractmethod
    def read(self, **kwargs: object) -> pl.LazyFrame:
        """Read data and return a Polars LazyFrame."""
        ...

    @abstractmethod
    def read_snapshot(self, snapshot_date: str, **kwargs: object) -> pl.LazyFrame:
        """Read a single point-in-time snapshot."""
        ...

    @abstractmethod
    def read_history(
        self,
        entity_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        **kwargs: object,
    ) -> pl.LazyFrame:
        """Read longitudinal history, optionally filtered by entity and date range."""
        ...
