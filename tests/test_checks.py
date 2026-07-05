"""Tests for built-in checks."""

from datetime import date

import polars as pl

from lens.checks.crosssource import CrossSourceMatchCheck
from lens.checks.snapshot import NullCheck, RangeCheck
from lens.checks.temporal import MonotonicityCheck, StaleDataCheck, VolatilityCheck


def _make_data() -> pl.LazyFrame:
    return pl.DataFrame(
        {
            "entity_id": ["L1"] * 5 + ["L2"] * 5,
            "snapshot_date": [date(2024, 1, d) for d in range(1, 6)] * 2,
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


def test_stale_data_trailing_freeze_flagged():
    """The canonical stale-feed failure: an entity that updated normally,
    then froze — whole-history constancy is NOT required."""
    df = pl.DataFrame(
        {
            "entity_id": ["L1"] * 6,
            "snapshot_date": [date(2024, 1, d) for d in range(1, 7)],
            "balance": [100, 200, 300, 300, 300, 300],  # trailing run of 4
        }
    ).lazy()
    check = StaleDataCheck(field="balance", max_unchanged=3)
    result = check.run(df)
    assert not result.passed
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.entity_id == "L1"
    assert issue.snapshot_date == date(2024, 1, 6)  # dated at the last snapshot
    assert issue.details["trailing_unchanged"] == 4


def test_stale_data_recently_changing_not_flagged():
    df = pl.DataFrame(
        {
            "entity_id": ["L1"] * 6,
            "snapshot_date": [date(2024, 1, d) for d in range(1, 7)],
            "balance": [300, 300, 300, 300, 300, 100],  # changed on the last snapshot
        }
    ).lazy()
    check = StaleDataCheck(field="balance", max_unchanged=3)
    result = check.run(df)
    assert result.passed


def test_volatility_from_zero_not_flagged():
    """0 → x transitions have an infinite relative change; fields that
    legitimately start at zero must not spam 'changed by inf%' findings."""
    df = pl.DataFrame(
        {
            "entity_id": ["L1"] * 3,
            "snapshot_date": [date(2024, 1, d) for d in range(1, 4)],
            "balance": [0.0, 500.0, 500.0],
        }
    ).lazy()
    check = VolatilityCheck(field="balance", max_pct_change=0.5)
    result = check.run(df)
    assert result.passed


def test_volatility_description_renders_percent():
    df = pl.DataFrame(
        {
            "entity_id": ["L1"] * 2,
            "snapshot_date": [date(2024, 1, 1), date(2024, 1, 2)],
            "balance": [1000.0, 1750.0],  # +75%
        }
    ).lazy()
    check = VolatilityCheck(field="balance", max_pct_change=0.5)
    result = check.run(df)
    assert len(result.issues) == 1
    assert "75.00%" in result.issues[0].description


def test_unknown_check_param_warns(caplog):
    """A YAML typo (z_treshold) must leave a trace, not silently run the
    check with default tuning."""
    import logging

    with caplog.at_level(logging.WARNING, logger="lens.checks.base"):
        VolatilityCheck(field="balance", max_pct_chnage=0.9)  # typo'd knob
    assert any("unknown parameter" in rec.message for rec in caplog.records)
    assert any("max_pct_chnage" in rec.message for rec in caplog.records)


def test_registry_collision_warns(caplog):
    import logging

    from lens.checks.base import BaseCheck
    from lens.checks.registry import CheckRegistry
    from lens.types import CheckResult

    reg = CheckRegistry()

    class _A(BaseCheck):
        name = "dup_check"

        def run(self, data, *, entity_col="entity_id", snapshot_col="snapshot_date"):
            return CheckResult(check_name=self.name, passed=True)

    class _B(BaseCheck):
        name = "dup_check"

        def run(self, data, *, entity_col="entity_id", snapshot_col="snapshot_date"):
            return CheckResult(check_name=self.name, passed=True)

    reg.register(_A)
    with caplog.at_level(logging.WARNING, logger="lens.checks.registry"):
        reg.register(_A)  # same class → silent (idempotent re-import)
    assert not caplog.records
    with caplog.at_level(logging.WARNING, logger="lens.checks.registry"):
        reg.register(_B)  # different class, same name → warns
    assert any("re-registered" in rec.message for rec in caplog.records)
