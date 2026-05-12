"""Map raw detector scores to (Severity, confidence) pairs.

Pure functions, no I/O, no global mutation. The orchestrator calls
`score_to_severity` once per Issue to translate detector-native scores
(anomaly probability, z-score, relative diff, ...) into the cross-detector
`(Severity, confidence)` representation carried on `Issue`.
"""

from __future__ import annotations

import logging
import math

from .types import Severity

logger = logging.getLogger(__name__)

# Per-detector threshold tables. Each list is sorted ASCENDING by threshold;
# the highest threshold whose value the raw score meets/exceeds wins. Below
# the lowest threshold falls through to `Severity.INFO`.
#
# Treat as read-only — copy via `dict(DEFAULT_THRESHOLDS)` if a caller needs
# to mutate.
DEFAULT_THRESHOLDS: dict[str, list[tuple[float, Severity]]] = {
    "tabpfn_anomaly": [
        (0.7, Severity.WARNING),
        (0.85, Severity.ERROR),
        (0.95, Severity.CRITICAL),
    ],
    "stl_residual": [
        (3.0, Severity.WARNING),
        (4.0, Severity.ERROR),
        (5.0, Severity.CRITICAL),
    ],
    "hierarchical_drill_down": [
        (3.0, Severity.WARNING),
        (4.0, Severity.ERROR),
        (5.0, Severity.CRITICAL),
    ],
    "cross_source_wiki": [
        (0.01, Severity.WARNING),
        (0.05, Severity.ERROR),
        (0.10, Severity.CRITICAL),
    ],
}


def _normalize_detector(detector: str) -> str:
    """Strip rule-slug suffix for keying: 'cross_source_wiki:rule_xyz' → 'cross_source_wiki'."""
    if ":" in detector:
        return detector.split(":", 1)[0]
    return detector


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _sigmoid_confidence(
    raw_score: float, thresholds: list[tuple[float, Severity]]
) -> float:
    """Sigmoid scaled around the WARNING threshold, saturating near CRITICAL.

    scale = (highest_threshold - warning_threshold) / 4 so the sigmoid hits
    ~0.98 at the highest threshold. Falls back to a reasonable default if the
    threshold table is degenerate (single entry).
    """
    warning_threshold = thresholds[0][0]
    highest_threshold = thresholds[-1][0]
    span = highest_threshold - warning_threshold
    if span <= 0.0:
        # Degenerate table: just use a fixed scale based on the warning threshold
        # magnitude (or 1.0 if zero) so the sigmoid is still well-defined.
        scale = abs(warning_threshold) if warning_threshold != 0.0 else 1.0
    else:
        scale = span / 4.0

    # Guard against scale==0 from a zero warning_threshold + single-entry table.
    if scale == 0.0:
        scale = 1.0

    # Numerically safe sigmoid.
    z = (raw_score - warning_threshold) / scale
    if z >= 0:
        ez = math.exp(-z)
        conf = 1.0 / (1.0 + ez)
    else:
        ez = math.exp(z)
        conf = ez / (1.0 + ez)
    return _clamp01(conf)


def score_to_severity(
    raw_score: float,
    detector: str,
    *,
    overrides: dict[str, list[tuple[float, Severity]]] | None = None,
) -> tuple[Severity, float]:
    """Map a raw detector score to `(Severity, confidence)`.

    Args:
        raw_score: Detector-native score (e.g. anomaly probability, z-score,
            relative diff). Larger = more anomalous for all built-in detectors.
        detector: Detector identifier. For cross-source-wiki rules, the form
            `cross_source_wiki:<rule_slug>` is normalized to `cross_source_wiki`
            before threshold lookup.
        overrides: If provided, fully REPLACES `DEFAULT_THRESHOLDS` for the
            lookup. Pass a complete table — there is no per-key merging.

    Returns:
        `(Severity, confidence)` where confidence is in [0, 1]. Unknown
        detectors return `(Severity.INFO, 0.0)` and emit a warning log.
    """
    table = overrides if overrides is not None else DEFAULT_THRESHOLDS
    key = _normalize_detector(detector)
    thresholds = table.get(key)
    if thresholds is None:
        logger.warning(
            "score_to_severity: unknown detector %r (normalized %r); "
            "returning (INFO, 0.0)",
            detector,
            key,
        )
        return (Severity.INFO, 0.0)

    # Walk ascending; pick the highest severity whose threshold is met.
    severity: Severity = Severity.INFO
    for threshold, sev in thresholds:
        if raw_score >= threshold:
            severity = sev
        else:
            break

    confidence = _sigmoid_confidence(raw_score, thresholds)
    return (severity, confidence)
