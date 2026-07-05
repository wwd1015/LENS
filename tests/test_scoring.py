"""Tests for `lens.scoring.score_to_severity`."""

from __future__ import annotations

import logging

import pytest

from lens.scoring import DEFAULT_THRESHOLDS, score_to_severity
from lens.types import Severity

# -----------------------------------------------------------------------------
# Per-detector severity bucketing.
#
# For each built-in detector, walk: below warning, at/just-above warning,
# at/just-above error, at/just-above critical.
# -----------------------------------------------------------------------------


_DETECTOR_CASES = [
    # (detector, below_warn, warn_threshold, err_threshold, crit_threshold)
    # tabpfn_anomaly is z-score scale, matching what the detector emits
    # ((observed − mean) / std, flagged at |z| > 3) — NOT a probability.
    ("tabpfn_anomaly", 1.5, 3.0, 4.0, 5.0),
    ("stl_residual", 1.5, 3.0, 4.0, 5.0),
    ("cross_source_wiki", 0.005, 0.01, 0.05, 0.10),
]


def test_tabpfn_thresholds_are_z_scale() -> None:
    """A freshly-flagged TabPFN anomaly (|z| just over 3) must be WARNING,
    not CRITICAL — the regression here was a probability-scale row that made
    every emitted anomaly CRITICAL with confidence ≈ 1.0."""
    sev, conf = score_to_severity(3.2, "tabpfn_anomaly")
    assert sev is Severity.WARNING
    assert conf < 0.9
    sev, _ = score_to_severity(5.5, "tabpfn_anomaly")
    assert sev is Severity.CRITICAL


@pytest.mark.parametrize(
    "detector,below_warn,warn,err,crit", _DETECTOR_CASES
)
def test_below_warning_is_info(
    detector: str, below_warn: float, warn: float, err: float, crit: float
) -> None:
    sev, conf = score_to_severity(below_warn, detector)
    assert sev is Severity.INFO
    assert 0.0 <= conf <= 1.0


@pytest.mark.parametrize(
    "detector,below_warn,warn,err,crit", _DETECTOR_CASES
)
def test_at_warning_is_warning(
    detector: str, below_warn: float, warn: float, err: float, crit: float
) -> None:
    sev, _ = score_to_severity(warn, detector)
    assert sev is Severity.WARNING


@pytest.mark.parametrize(
    "detector,below_warn,warn,err,crit", _DETECTOR_CASES
)
def test_just_above_warning_is_warning(
    detector: str, below_warn: float, warn: float, err: float, crit: float
) -> None:
    # Halfway between warning and error → still WARNING.
    midpoint = (warn + err) / 2.0
    sev, _ = score_to_severity(midpoint, detector)
    assert sev is Severity.WARNING


@pytest.mark.parametrize(
    "detector,below_warn,warn,err,crit", _DETECTOR_CASES
)
def test_at_error_is_error(
    detector: str, below_warn: float, warn: float, err: float, crit: float
) -> None:
    sev, _ = score_to_severity(err, detector)
    assert sev is Severity.ERROR


@pytest.mark.parametrize(
    "detector,below_warn,warn,err,crit", _DETECTOR_CASES
)
def test_just_above_error_is_error(
    detector: str, below_warn: float, warn: float, err: float, crit: float
) -> None:
    midpoint = (err + crit) / 2.0
    sev, _ = score_to_severity(midpoint, detector)
    assert sev is Severity.ERROR


@pytest.mark.parametrize(
    "detector,below_warn,warn,err,crit", _DETECTOR_CASES
)
def test_at_critical_is_critical(
    detector: str, below_warn: float, warn: float, err: float, crit: float
) -> None:
    sev, _ = score_to_severity(crit, detector)
    assert sev is Severity.CRITICAL


@pytest.mark.parametrize(
    "detector,below_warn,warn,err,crit", _DETECTOR_CASES
)
def test_well_above_critical_is_critical(
    detector: str, below_warn: float, warn: float, err: float, crit: float
) -> None:
    sev, conf = score_to_severity(crit * 1.5 + 0.01, detector)
    assert sev is Severity.CRITICAL
    assert 0.0 <= conf <= 1.0


# -----------------------------------------------------------------------------
# Confidence monotonicity within the meaningful range.
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("detector", ["tabpfn_anomaly", "stl_residual", "cross_source_wiki"])
def test_confidence_monotonically_increases(detector: str) -> None:
    thresholds = DEFAULT_THRESHOLDS[detector]
    lo = thresholds[0][0]  # warning
    hi = thresholds[-1][0]  # critical
    # Sample 20 points across [lo - span, hi + span].
    span = hi - lo
    samples = [lo - span + (i * (3 * span) / 19) for i in range(20)]
    confs = [score_to_severity(s, detector)[1] for s in samples]
    # Strictly non-decreasing.
    for a, b in zip(confs, confs[1:]):
        assert a <= b + 1e-12, f"confidence not monotonic for {detector}: {confs}"
    # And in the meaningful range, strictly increasing between lo and hi.
    mid_samples = [lo + (i * span / 9) for i in range(10)]
    mid_confs = [score_to_severity(s, detector)[1] for s in mid_samples]
    for a, b in zip(mid_confs, mid_confs[1:]):
        assert a < b, f"confidence not strictly increasing in middle for {detector}: {mid_confs}"


@pytest.mark.parametrize("detector", ["tabpfn_anomaly", "stl_residual", "cross_source_wiki"])
def test_confidence_in_unit_interval(detector: str) -> None:
    thresholds = DEFAULT_THRESHOLDS[detector]
    lo = thresholds[0][0]
    hi = thresholds[-1][0]
    samples = [-1e6, lo - 1.0, lo, (lo + hi) / 2, hi, hi + 1e6]
    for s in samples:
        _, conf = score_to_severity(s, detector)
        assert 0.0 <= conf <= 1.0, f"conf out of [0,1] for {detector}@{s}: {conf}"


@pytest.mark.parametrize("detector", ["tabpfn_anomaly", "stl_residual", "cross_source_wiki"])
def test_confidence_half_at_warning_threshold(detector: str) -> None:
    """Sigmoid is centered on the warning threshold → conf(warn) ≈ 0.5."""
    thresholds = DEFAULT_THRESHOLDS[detector]
    warn = thresholds[0][0]
    _, conf = score_to_severity(warn, detector)
    assert abs(conf - 0.5) < 1e-9


# -----------------------------------------------------------------------------
# Unknown detector handling.
# -----------------------------------------------------------------------------


def test_unknown_detector_returns_info_zero(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="lens.scoring"):
        sev, conf = score_to_severity(0.99, "no_such_detector")
    assert sev is Severity.INFO
    assert conf == 0.0
    assert any("unknown detector" in rec.message for rec in caplog.records)


def test_unknown_detector_high_score_still_info() -> None:
    # Confirm no fall-through to any other detector's table.
    sev, conf = score_to_severity(1_000_000.0, "totally_made_up")
    assert sev is Severity.INFO
    assert conf == 0.0


# -----------------------------------------------------------------------------
# Cross-source-wiki rule-slug normalization.
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_score,expected_severity",
    [
        (0.005, Severity.INFO),
        (0.01, Severity.WARNING),
        (0.05, Severity.ERROR),
        (0.10, Severity.CRITICAL),
    ],
)
def test_cross_source_wiki_rule_slug_normalized(
    raw_score: float, expected_severity: Severity
) -> None:
    base_sev, base_conf = score_to_severity(raw_score, "cross_source_wiki")
    slug_sev, slug_conf = score_to_severity(raw_score, "cross_source_wiki:rule_xyz")
    assert base_sev is expected_severity
    assert slug_sev is expected_severity
    assert base_conf == pytest.approx(slug_conf)


def test_cross_source_wiki_arbitrary_suffix_normalized() -> None:
    s1, c1 = score_to_severity(0.07, "cross_source_wiki")
    s2, c2 = score_to_severity(0.07, "cross_source_wiki:some_very_long_rule_name_123")
    assert s1 is s2
    assert c1 == pytest.approx(c2)


# -----------------------------------------------------------------------------
# Overrides parameter.
# -----------------------------------------------------------------------------


def test_overrides_replaces_default_table() -> None:
    overrides = {
        "my_detector": [
            (10.0, Severity.WARNING),
            (20.0, Severity.ERROR),
            (30.0, Severity.CRITICAL),
        ]
    }
    sev, conf = score_to_severity(15.0, "my_detector", overrides=overrides)
    assert sev is Severity.WARNING
    assert 0.0 < conf < 1.0

    sev, _ = score_to_severity(25.0, "my_detector", overrides=overrides)
    assert sev is Severity.ERROR

    sev, _ = score_to_severity(35.0, "my_detector", overrides=overrides)
    assert sev is Severity.CRITICAL

    sev, _ = score_to_severity(5.0, "my_detector", overrides=overrides)
    assert sev is Severity.INFO


def test_overrides_hides_default_detectors() -> None:
    # When overrides is provided, default keys are NOT consulted.
    overrides = {
        "only_me": [(1.0, Severity.WARNING), (2.0, Severity.CRITICAL)],
    }
    sev, conf = score_to_severity(0.99, "tabpfn_anomaly", overrides=overrides)
    assert sev is Severity.INFO
    assert conf == 0.0


def test_overrides_respects_cross_source_normalization() -> None:
    overrides = {
        "cross_source_wiki": [
            (0.5, Severity.WARNING),
            (1.0, Severity.ERROR),
            (2.0, Severity.CRITICAL),
        ]
    }
    sev_a, _ = score_to_severity(1.5, "cross_source_wiki", overrides=overrides)
    sev_b, _ = score_to_severity(1.5, "cross_source_wiki:rule_q", overrides=overrides)
    assert sev_a is Severity.ERROR
    assert sev_b is Severity.ERROR


# -----------------------------------------------------------------------------
# DEFAULT_THRESHOLDS is not mutated by calls.
# -----------------------------------------------------------------------------


def test_default_thresholds_not_mutated() -> None:
    snapshot = {k: list(v) for k, v in DEFAULT_THRESHOLDS.items()}
    score_to_severity(0.99, "tabpfn_anomaly")
    score_to_severity(0.005, "cross_source_wiki:rule_x")
    score_to_severity(100.0, "unknown_thing")
    score_to_severity(2.0, "my_detector", overrides={"my_detector": [(1.0, Severity.WARNING)]})
    assert {k: list(v) for k, v in DEFAULT_THRESHOLDS.items()} == snapshot
