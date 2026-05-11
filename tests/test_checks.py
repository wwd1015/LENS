"""Tests for built-in checks."""

from datetime import date

import polars as pl

from lens.checks.crosssource import CrossSourceMatchCheck
from lens.checks.snapshot import NullCheck, RangeCheck
from lens.checks.temporal import MonotonicityCheck, StaleDataCheck, VolatilityCheck
from lens.types import Severity


def _make_data() -> pl.LazyFrame:
    return pl.DataFrame(
        {
            "entity_id": ["L1"] * 5 + ["L2"] * 5,
            "snapshot_date": [
                date(2024, 1, d) for d in range(1, 6)
            ]
            * 2,
            "balance": [1000, 950, 900, 850, 800, 500, 500, 500, 500, 500],
            "status": ["current"] * 5 + ["current"] * 4 + [None],
        }
    ).lazy()


def test_null_check():
    data = _make_data()
    check = NullCheck(fields=["status"])
    result = check.run(data)
    assert not result.passed
    assert len(result.issues) == 1
    assert result.issues[0].entity_id == "L2"


def test_range_check():
    data = _make_data()
    check = RangeCheck(field="balance", min_value=0, max_value=900)
    result = check.run(data)
    assert not result.passed
    assert all(i.field_name == "balance" for i in result.issues)


def test_stale_data():
    data = _make_data()
    check = StaleDataCheck(field="balance", max_unchanged=3)
    result = check.run(data)
    assert not result.passed
    assert len(result.issues) == 1
    assert result.issues[0].entity_id == "L2"


def test_monotonicity():
    data = _make_data()
    check = MonotonicityCheck(field="balance", direction="decreasing")
    result = check.run(data)
    # L1 is strictly decreasing, L2 is flat — flat is not a violation for decreasing
    assert result.passed


def test_monotonicity_violation():
    df = pl.DataFrame(
        {
            "entity_id": ["L1"] * 3,
            "snapshot_date": [date(2024, 1, d) for d in range(1, 4)],
            "balance": [100, 200, 150],
        }
    ).lazy()
    check = MonotonicityCheck(field="balance", direction="decreasing")
    result = check.run(df)
    assert not result.passed


def test_volatility():
    df = pl.DataFrame(
        {
            "entity_id": ["L1"] * 3,
            "snapshot_date": [date(2024, 1, d) for d in range(1, 4)],
            "balance": [1000, 1000, 2000],
        }
    ).lazy()
    check = VolatilityCheck(field="balance", max_pct_change=0.5)
    result = check.run(df)
    assert not result.passed
    assert len(result.issues) == 1


def test_cross_source_match():
    source_a = pl.DataFrame(
        {
            "entity_id": ["L1", "L2"],
            "snapshot_date": [date(2024, 1, 1)] * 2,
            "balance": [1000.0, 500.0],
        }
    ).lazy()
    source_b = pl.DataFrame(
        {
            "entity_id": ["L1", "L2"],
            "snapshot_date": [date(2024, 1, 1)] * 2,
            "balance": [1000.0, 510.0],
        }
    ).lazy()

    check = CrossSourceMatchCheck(field="balance", tolerance=5.0, tolerance_type="absolute")
    result = check.run_cross(
        source_a, source_b, source_a_name="loan_system", source_b_name="accounting"
    )
    assert not result.passed
    assert len(result.issues) == 1
    assert result.issues[0].entity_id == "L2"
