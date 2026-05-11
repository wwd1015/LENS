"""In-memory / file-based Polars data source."""

from __future__ import annotations

import polars as pl

from lens.io.base import DataSource


class PolarsSource(DataSource):
    """Data source backed by an in-memory Polars DataFrame or LazyFrame.

    Also supports reading from CSV/Parquet files.
    """

    def __init__(
        self,
        data: pl.DataFrame | pl.LazyFrame | None = None,
        path: str | None = None,
        *,
        snapshot_col: str = "snapshot_date",
        entity_col: str = "entity_id",
    ) -> None:
        self.snapshot_col = snapshot_col
        self.entity_col = entity_col

        if data is not None:
            self._lf = data.lazy() if isinstance(data, pl.DataFrame) else data
        elif path is not None:
            if path.endswith(".parquet"):
                self._lf = pl.scan_parquet(path)
            elif path.endswith(".csv"):
                self._lf = pl.scan_csv(path)
            else:
                raise ValueError(f"Unsupported file format: {path}")
        else:
            raise ValueError("Must provide either 'data' or 'path'.")

    def read(self, **kwargs: object) -> pl.LazyFrame:
        return self._lf

    def read_snapshot(self, snapshot_date: str, **kwargs: object) -> pl.LazyFrame:
        return self._lf.filter(pl.col(self.snapshot_col) == snapshot_date)

    def read_history(
        self,
        entity_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        **kwargs: object,
    ) -> pl.LazyFrame:
        lf = self._lf
        if entity_id is not None:
            lf = lf.filter(pl.col(self.entity_col) == entity_id)
        if start_date is not None:
            lf = lf.filter(pl.col(self.snapshot_col) >= start_date)
        if end_date is not None:
            lf = lf.filter(pl.col(self.snapshot_col) <= end_date)
        return lf
