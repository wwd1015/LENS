"""Tests for TabPFNAnomalyCheck.

Uses an injected stub forecaster so the heavy ``tabpfn-time-series`` extra
is not required for unit tests.
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta

import polars as pl

from lens.checks.tabpfn_anomaly import TabPFNAnomalyCheck


def _stub_forecaster(history: list[float]) -> tuple[float, float]:
    """Deterministic stand-in for TabPFN-TS: rolling mean + sample stdev.

    Good enough for testing the check's plumbing; the real forecaster is
    swapped in via the ``[tabpfn]`` extra at runtime.
    """
    mean = statistics.fmean(history)
    std = statistics.pstdev(history) or 1.0
    return mean, std


def _series(entity: str, values: list[float]) -> pl.LazyFrame:
    base = date(2024, 1, 1)
    return pl.DataFrame(
        {
            "entity_id": [entity] * len(values),
            "snapshot_date": [base + timedelta(days=i) for i in range(len(values))],
            "balance": values,
        }
    ).lazy()


def test_clean_series_passes():
    data = _series("L1", [100.0 + i * 0.1 for i in range(40)])
    check = TabPFNAnomalyCheck(
        field="balance",
        context_window=30,
        score_threshold=3.0,
        min_history=20,
        forecaster=_stub_forecaster,
    )
    result = check.run(data)
    assert result.passed
    assert result.issues == []


def test_injected_spike_is_flagged():
    values = [100.0 + i * 0.1 for i in range(39)] + [10_000.0]  # last point is wildly off
    data = _series("L1", values)
    check = TabPFNAnomalyCheck(
        field="balance",
        context_window=30,
        score_threshold=3.0,
        min_history=20,
        forecaster=_stub_forecaster,
    )
    result = check.run(data)
    assert not result.passed
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.entity_id == "L1"
    assert issue.field_name == "balance"
    assert issue.details["observed"] == 10_000.0
    assert abs(issue.details["score"]) > 3.0


def test_short_history_skipped():
    data = _series("L1", [100.0, 101.0, 102.0])  # below min_history
    check = TabPFNAnomalyCheck(
        field="balance",
        min_history=20,
        forecaster=_stub_forecaster,
    )
    result = check.run(data)
    assert result.passed


def test_per_entity_isolation():
    clean = _series("L1", [100.0 + i * 0.1 for i in range(40)])
    spiked = _series("L2", [100.0 + i * 0.1 for i in range(39)] + [-5_000.0])
    data = pl.concat([clean, spiked])
    check = TabPFNAnomalyCheck(
        field="balance",
        context_window=30,
        score_threshold=3.0,
        min_history=20,
        forecaster=_stub_forecaster,
    )
    result = check.run(data)
    assert not result.passed
    flagged_ids = {i.entity_id for i in result.issues}
    assert flagged_ids == {"L2"}


def test_missing_extra_raises_clear_error():
    """If TabPFN-TS isn't installed, calling without an injected forecaster
    must raise ImportError pointing to the [tabpfn] extra."""
    import sys

    if "tabpfn_time_series" in sys.modules:
        # Extra is installed; nothing to assert about the error path.
        return
    check = TabPFNAnomalyCheck(field="balance", min_history=2, forecaster=None)
    data = _series("L1", [1.0, 2.0, 3.0, 4.0, 5.0])
    try:
        check.run(data)
    except ImportError as e:
        assert "[tabpfn]" in str(e)
        return
    raise AssertionError("Expected ImportError when [tabpfn] extra missing")
