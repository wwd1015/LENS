"""Tests for the Suite engine and config loading."""

from datetime import date
from pathlib import Path
from textwrap import dedent

import polars as pl

from lens.config import load_suite
from lens.engine import Suite
from lens.io import PolarsSource


def _sample_data() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "loan_id": ["L1"] * 4 + ["L2"] * 4,
            "as_of_date": [date(2024, 1, d) for d in range(1, 5)] * 2,
            "balance": [1000, 950, 900, 850, 500, 500, 500, 500],
        }
    )


def test_suite_from_code():
    df = _sample_data()
    source = PolarsSource(data=df, entity_col="loan_id", snapshot_col="as_of_date")

    suite = Suite(entity_col="loan_id", snapshot_col="as_of_date")
    suite.add("null_check", fields=["balance"])
    suite.add("stale_data", field="balance", max_unchanged=2)

    result = suite.run(source)
    assert len(result.results) == 2
    # null_check should pass (no nulls)
    assert result.results[0].passed
    # stale_data should flag L2
    assert not result.results[1].passed


def test_suite_from_yaml(tmp_path: Path):
    config = dedent("""\
        entity_col: loan_id
        snapshot_col: as_of_date
        checks:
          - name: null_check
            params:
              fields: [balance]
          - name: stale_data
            severity: error
            params:
              field: balance
              max_unchanged: 2
    """)
    config_file = tmp_path / "suite.yaml"
    config_file.write_text(config)

    suite = load_suite(config_file)
    df = _sample_data()
    result = suite.run(df)
    assert len(result.results) == 2
