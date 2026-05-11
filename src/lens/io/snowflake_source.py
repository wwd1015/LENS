"""Snowflake data source connector."""

from __future__ import annotations

from typing import Any

import polars as pl

from lens.io.base import DataSource


class SnowflakeSource(DataSource):
    """Data source that reads from Snowflake via ConnectorX (polars native).

    Requires the ``snowflake`` extra: ``pip install lens-dq[snowflake]``
    """

    def __init__(
        self,
        connection_uri: str,
        table: str | None = None,
        query: str | None = None,
        *,
        snapshot_col: str = "snapshot_date",
        entity_col: str = "entity_id",
    ) -> None:
        self.connection_uri = connection_uri
        self.table = table
        self.query = query
        self.snapshot_col = snapshot_col
        self.entity_col = entity_col

        if table is None and query is None:
            raise ValueError("Must provide either 'table' or 'query'.")

    def _base_query(self) -> str:
        if self.query:
            return self.query
        return f"SELECT * FROM {self.table}"  # noqa: S608

    def _read_sql(self, query: str) -> pl.LazyFrame:
        return pl.read_database_uri(query, uri=self.connection_uri).lazy()

    def read(self, **kwargs: Any) -> pl.LazyFrame:
        return self._read_sql(self._base_query())

    def read_snapshot(self, snapshot_date: str, **kwargs: Any) -> pl.LazyFrame:
        q = f"SELECT * FROM ({self._base_query()}) t WHERE {self.snapshot_col} = '{snapshot_date}'"  # noqa: S608
        return self._read_sql(q)

    def read_history(
        self,
        entity_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        **kwargs: Any,
    ) -> pl.LazyFrame:
        conditions: list[str] = []
        if entity_id is not None:
            conditions.append(f"{self.entity_col} = '{entity_id}'")
        if start_date is not None:
            conditions.append(f"{self.snapshot_col} >= '{start_date}'")
        if end_date is not None:
            conditions.append(f"{self.snapshot_col} <= '{end_date}'")

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        q = f"SELECT * FROM ({self._base_query()}) t{where}"  # noqa: S608
        return self._read_sql(q)
