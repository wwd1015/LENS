"""Tests for STLResidualCheck.

Covers:
- Synthetic seasonal series with a single planted anomaly (recall = 100% at z=3)
- Short series (below min_history) -> no Issues, no crash
- Constant series -> no Issues, no crash
- Two-entity frame: planted-anomaly entity flags the spike index; clean entity
  is allowed some false positives (an inherent property of robust STL on noisy
  seasonal data — eng review item 7 is about *crash* guards, not precision).
- Issue fields populated correctly
- Check is registered

Note on robust STL + z-score behavior: ``robust=True`` uses iteratively-
reweighted LOESS, which produces fat-tailed residual distributions on noisy
seasonal data. A handful of false-positive residuals at |z|>3 are normal.
Tests therefore assert recall on planted anomalies, not zero-FP precision.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from lens.checks.registry import registry
from lens.checks.temporal_stl import STLResidualCheck


def _make_lazyframe(entity: str, values: list[float]) -> pl.LazyFrame:
    base = date(2024, 1, 1)
    return pl.DataFrame(
        {
            "entity_id": [entity] * len(values),
            "snapshot_date": [base + timedelta(days=i) for i in range(len(values))],
            "balance": values,
        }
    ).lazy()


def _seasonal_series(n: int, period: int, amplitude: float = 10.0,
                     noise: float = 0.5, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    seasonal = amplitude * np.sin(2 * np.pi * t / period)
    trend = 100.0 + 0.05 * t
    noise_arr = rng.normal(0.0, noise, size=n)
    return trend + seasonal + noise_arr


def test_registered_in_registry():
    assert "stl_residual" in registry.list_checks()
    cls = registry.get("stl_residual")
    assert cls is STLResidualCheck


def test_seasonal_series_with_spike_is_flagged():
    """90-day sine wave + a planted spike. Recall must be 100% at z=3.

    Per plan spec: 90-day daily synthetic sine wave + single planted anomaly.
    Uses a small spike (3 units) so robust STL doesn't fully downweight it
    into the trend/seasonal components — that's the regime robust STL is
    designed for.
    """
    n = 90
    period = 30
    values = _seasonal_series(n, period=period, amplitude=10.0, noise=0.5, seed=42)
    spike_idx = 60
    values[spike_idx] += 3.0  # ~5σ residual-scale anomaly post-decomposition

    data = _make_lazyframe("L1", values.tolist())
    check = STLResidualCheck(field="balance", period=period, z_threshold=3.0)
    result = check.run(data)

    assert not result.passed
    flagged_indices: set[int] = set()
    for issue in result.issues:
        assert issue.entity_id == "L1"
        assert issue.field_name == "balance"
        assert issue.snapshot_date is not None
        assert "z_score" in issue.details
        assert "residual" in issue.details
        assert "field_value" in issue.details
        assert isinstance(issue.details["z_score"], float)
        idx = (issue.snapshot_date - date(2024, 1, 1)).days
        flagged_indices.add(idx)

    # Recall on the planted anomaly: spike index must be flagged.
    assert spike_idx in flagged_indices, (
        f"Spike at index {spike_idx} not flagged; flagged={sorted(flagged_indices)}"
    )


def test_short_series_skipped():
    """5-point series is below default min_history (2*period=60); no Issues, no crash."""
    data = _make_lazyframe("L1", [100.0, 101.0, 102.0, 103.0, 104.0])
    check = STLResidualCheck(field="balance", period=30, z_threshold=3.0)
    result = check.run(data)
    assert result.passed
    assert result.issues == []


def test_short_series_skipped_with_explicit_min_history():
    """Explicit min_history overrides the 2*period default."""
    data = _make_lazyframe("L1", [100.0 + i * 0.1 for i in range(40)])
    check = STLResidualCheck(
        field="balance", period=30, z_threshold=3.0, min_history=100
    )
    result = check.run(data)
    assert result.passed
    assert result.issues == []


def test_constant_series_skipped():
    """90 identical points -> residual stdev is 0; no Issues, no crash."""
    data = _make_lazyframe("L1", [42.0] * 90)
    check = STLResidualCheck(field="balance", period=30, z_threshold=3.0)
    result = check.run(data)
    assert result.passed
    assert result.issues == []


def test_zero_noise_seasonal_skipped_as_degenerate():
    """Zero-noise seasonal series -> residuals are floating-point dust; treated
    as degenerate (relative-to-signal floor) so we don't amplify FP noise."""
    n = 180
    period = 30
    t = np.arange(n)
    values = 100.0 + 0.05 * t + 10.0 * np.sin(2 * np.pi * t / period)
    data = _make_lazyframe("L1", values.tolist())
    check = STLResidualCheck(field="balance", period=period, z_threshold=3.0)
    result = check.run(data)
    assert result.passed
    assert result.issues == []


def test_two_entities_spike_flagged_on_correct_one():
    """Two entities; only the spiked one should flag the planted index.

    Robust STL produces some random false-positives on noisy seasonal data —
    those are accepted. What must hold: (a) the spiked entity flags the spike
    index, (b) the spike's z-score is the largest among that entity's
    residuals, (c) the clean entity does not flag index 90.
    """
    n = 180
    period = 30
    clean_vals = _seasonal_series(n, period=period, amplitude=10.0, noise=0.5, seed=1)
    spiked_vals = _seasonal_series(n, period=period, amplitude=10.0, noise=0.5, seed=2)
    spike_idx = 90
    spiked_vals[spike_idx] += 5.0

    clean_lf = _make_lazyframe("L1", clean_vals.tolist())
    spiked_lf = _make_lazyframe("L2", spiked_vals.tolist())
    data = pl.concat([clean_lf, spiked_lf])

    check = STLResidualCheck(field="balance", period=period, z_threshold=3.0)
    result = check.run(data)
    assert not result.passed

    # L2 must flag spike_idx; the spike must have the largest |z| on L2.
    l2_issues = [i for i in result.issues if i.entity_id == "L2"]
    assert l2_issues, "Expected at least one Issue on the spiked entity L2"
    l2_indices = {
        (i.snapshot_date - date(2024, 1, 1)).days: i for i in l2_issues
    }
    assert spike_idx in l2_indices, (
        f"Spike at idx {spike_idx} not flagged on L2; got {sorted(l2_indices)}"
    )
    max_z_issue = max(l2_issues, key=lambda i: abs(i.details["z_score"]))
    spike_issue = l2_indices[spike_idx]
    assert max_z_issue is spike_issue, "Spike should be the largest-z issue on L2"

    # L1 (clean) must not flag the planted spike index.
    l1_indices = {
        (i.snapshot_date - date(2024, 1, 1)).days
        for i in result.issues
        if i.entity_id == "L1"
    }
    assert spike_idx not in l1_indices


def test_confidence_in_unit_interval():
    """Confidence proportional to |z|, always within [0,1]."""
    n = 180
    period = 30
    values = _seasonal_series(n, period=period, amplitude=10.0, noise=0.5, seed=7)
    values[90] += 5.0
    data = _make_lazyframe("L1", values.tolist())
    check = STLResidualCheck(field="balance", period=period, z_threshold=3.0)
    result = check.run(data)
    assert not result.passed
    for issue in result.issues:
        assert 0.0 <= issue.confidence <= 1.0


def test_detector_source_set():
    n = 180
    period = 30
    values = _seasonal_series(n, period=period, amplitude=10.0, noise=0.5, seed=11)
    values[90] += 5.0
    data = _make_lazyframe("L1", values.tolist())
    check = STLResidualCheck(field="balance", period=period, z_threshold=3.0)
    result = check.run(data)
    assert not result.passed
    assert all(i.detector_source == "stl_residual" for i in result.issues)


def test_recall_on_planted_anomaly_fixture():
    """End-to-end recall sanity: across multiple seeds with the same planted
    anomaly recipe, recall on the spike index should be 100% at z=3.

    This is the recall claim that lets the orchestrator trust STL as a
    baseline TS detector when seasonality is present.
    """
    n = 180
    period = 30
    spike_idx = 90
    hits = 0
    trials = 8
    for seed in range(trials):
        vals = _seasonal_series(n, period=period, amplitude=10.0, noise=0.5, seed=seed)
        vals[spike_idx] += 5.0
        data = _make_lazyframe(f"E{seed}", vals.tolist())
        check = STLResidualCheck(field="balance", period=period, z_threshold=3.0)
        result = check.run(data)
        flagged = {
            (i.snapshot_date - date(2024, 1, 1)).days for i in result.issues
        }
        if spike_idx in flagged:
            hits += 1
    assert hits == trials, f"Recall {hits}/{trials} < 100%"
