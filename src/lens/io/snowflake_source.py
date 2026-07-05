"""Snowflake data source connector."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import polars as pl

from lens.io.base import DataSource

# Plain (optionally dotted / $-suffixed) SQL identifiers, or a
# double-quoted identifier with no embedded quotes (Snowflake's escape for
# mixed case / spaces). Anything else is rejected rather than risk
# interpolating attacker-shaped text into a query.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.$]*$")
_QUOTED_IDENTIFIER_RE = re.compile(r'^"[^"]+"$')


def _validate_identifier(name: str, *, what: str) -> str:
    """Return ``name`` if it is a plain or quoted SQL identifier, else raise."""
    if not (_IDENTIFIER_RE.match(name or "") or _QUOTED_IDENTIFIER_RE.match(name or "")):
        raise ValueError(f"{what} {name!r} is not a plain SQL identifier")
    return name


def _quote_value(value: str) -> str:
    """Render a string as a single-quoted SQL literal, escaping quotes.

    ``entity_id`` flows in from data and from analyst input
    (``lens-rca --investigate-entity``), so it must never be interpolated raw.
    """
    return "'" + str(value).replace("'", "''") + "'"


def _quote_date(value: str, *, what: str) -> str:
    """Validate a date/datetime string and re-emit it as a quoted ISO literal."""
    text = str(value)
    parsed: date | datetime
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{what} {value!r} is not an ISO date/datetime") from exc
    return f"'{parsed.isoformat()}'"


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
        self.table = _validate_identifier(table, what="table") if table is not None else None
        self.query = query
        self.snapshot_col = _validate_identifier(snapshot_col, what="snapshot_col")
        self.entity_col = _validate_identifier(entity_col, what="entity_col")

        if table is None and query is None:
            raise ValueError("Must provide either 'table' or 'query'.")

    def _base_query(self) -> str:
        if self.query:
            return self.query
        return f"SELECT * FROM {self.table}"  # noqa: S608 - identifier validated in __init__

    def _read_sql(self, query: str) -> pl.LazyFrame:
        return pl.read_database_uri(query, uri=self.connection_uri).lazy()

    def read(self, **kwargs: Any) -> pl.LazyFrame:
        return self._read_sql(self._base_query())

    def read_snapshot(self, snapshot_date: str, **kwargs: Any) -> pl.LazyFrame:
        q = self._snapshot_query(snapshot_date)
        return self._read_sql(q)

    def _snapshot_query(self, snapshot_date: str) -> str:
        literal = _quote_date(snapshot_date, what="snapshot_date")
        return (
            f"SELECT * FROM ({self._base_query()}) t "  # noqa: S608 - values quoted/validated
            f"WHERE {self.snapshot_col} = {literal}"
        )

    def read_history(
        self,
        entity_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        **kwargs: Any,
    ) -> pl.LazyFrame:
        q = self._history_query(entity_id, start_date, end_date)
        return self._read_sql(q)

    def _history_query(
        self,
        entity_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        conditions: list[str] = []
        if entity_id is not None:
            conditions.append(f"{self.entity_col} = {_quote_value(entity_id)}")
        if start_date is not None:
            start = _quote_date(start_date, what="start_date")
            conditions.append(f"{self.snapshot_col} >= {start}")
        if end_date is not None:
            end = _quote_date(end_date, what="end_date")
            conditions.append(f"{self.snapshot_col} <= {end}")

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        return f"SELECT * FROM ({self._base_query()}) t{where}"  # noqa: S608 - values quoted/validated
