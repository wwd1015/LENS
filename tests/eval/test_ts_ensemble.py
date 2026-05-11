"""TS ensemble eval (T13).

Compares three TS detectors — TabPFN-TS (optional), STL-residual, and an inline
rolling-Z — across three synthetic fixtures with labeled point anomalies. Tests
whether a simple score-average / vote ensemble outperforms the best single
detector.

Marked ``@pytest.mark.eval`` for *grouping consistency* with the other v2
evals, but unlike the LLM evals this is pure CPU + math, so it runs whenever
the user invokes ``pytest -m eval`` — no ``LENS_RUN_EVAL=1`` gate.

Pass criterion (per eng review item "TS ensemble eval pass criterion is too
weak"): ensemble recall@1 ≥ max(individual recall@1) on every fixture, AND
strictly better on at least 1 of 3 fixtures. If this fails, the
recommendation is to ship the MVP with detector-routing (one detector per
data shape — TabPFN/rolling-Z for event-driven, STL for seasonal) rather
than a score-averaged ensemble. Defer ensembling to v2.1.

If ``tabpfn_time_series`` is not installed, the eval runs with STL +
rolling-Z only. The strict-better criterion in that case becomes: vote=≥2/2
(i.e. both agree) recall@1 strictly better than either single on at least
1 fixture.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from lens.checks.temporal_stl import STLResidualCheck

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ts_incidents"
FIXTURES = ["event_driven", "seasonal", "mixed"]

logger = logging.getLogger(__name__)

# Per-fixture STL period choice. event_driven has no real seasonality so we
# pick the smallest period that lets STL fit (period=7, min_history=14).
# seasonal and mixed both have a weekly cycle.
STL_PERIOD = {"event_driven": 7, "seasonal": 7, "mixed": 7}

# Rolling-Z window. 14 points (~2 weekly cycles) — long enough that the
# rolling mean/std reflect the local regime but short enough to react to
# point anomalies.
ROLLING_WINDOW = 14
# Rolling-Z threshold of 2.0 (looser than the canonical 3.0). Rationale: this
# eval scores top-1 retrieval, not zero-FP precision. We want each detector to
# emit a *candidate set* — false positives are fine, missed positives are not.
# The ensemble's AND filtering handles FP rejection. At threshold 3.0 RZ misses
# the seasonal fixture spike entirely, defeating the experiment.
ROLLING_Z_THRESHOLD = 2.0


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> tuple[pl.LazyFrame, int, list[date]]:
    df = pl.read_csv(FIXTURE_DIR / f"{name}.csv")
    df = df.with_columns(pl.col("snapshot_date").str.to_date())
    df = df.with_columns(pl.lit("E0").alias("entity_id"))
    df = df.select("entity_id", "snapshot_date", "balance")
    labels = json.loads((FIXTURE_DIR / "labels.json").read_text())
    anomaly_idx = labels[name]["anomaly_index"]
    snapshots = df["snapshot_date"].to_list()
    return df.lazy(), anomaly_idx, snapshots


# ---------------------------------------------------------------------------
# Detectors — each returns {index: score}. Higher score = more anomalous.
# ---------------------------------------------------------------------------


def _stl_scores(lf: pl.LazyFrame, period: int) -> dict[int, float]:
    """Run STLResidualCheck and return {index: |z_score|} for flagged points.

    Uses z_threshold=2.0 (looser than default) so we get a usable scoreboard
    on event_driven (where STL is not the right detector but we still want
    to compare its best guess against ground truth).
    """
    check = STLResidualCheck(
        field="balance",
        period=period,
        z_threshold=2.0,
        min_history=2 * period,
    )
    result = check.run(lf)
    out: dict[int, float] = {}
    base = date(2024, 1, 1)
    for issue in result.issues:
        idx = (issue.snapshot_date - base).days
        out[idx] = abs(float(issue.details["z_score"]))
    return out


def _rolling_z_scores(lf: pl.LazyFrame, window: int) -> dict[int, float]:
    """Inline rolling-Z: |x - rolling_mean| / rolling_std over a window.

    Returns ``{index: |z|}`` for indices where |z| > ROLLING_Z_THRESHOLD.
    No new check class — this is pure numpy because that's all rolling-Z is.
    Uses a left-justified ("trailing") window: at index i, we use points
    [i-window+1, i]. NaN for the first ``window-1`` indices and for any
    window with zero stdev.
    """
    df = lf.sort("snapshot_date").collect()
    values = np.asarray(df["balance"].to_list(), dtype=float)
    n = len(values)
    out: dict[int, float] = {}
    for i in range(window - 1, n):
        chunk = values[i - window + 1 : i + 1]
        mu = float(np.mean(chunk))
        sigma = float(np.std(chunk, ddof=0))
        if sigma <= 1e-12:
            continue
        z = (values[i] - mu) / sigma
        if abs(z) > ROLLING_Z_THRESHOLD:
            out[i] = abs(z)
    return out


def _tabpfn_scores(lf: pl.LazyFrame) -> dict[int, float] | None:
    """If ``tabpfn_time_series`` is importable, run TabPFNAnomalyCheck and
    return scored anomalies. Else return None (caller treats as "skip")."""
    try:
        import tabpfn_time_series  # noqa: F401
    except Exception:
        return None
    # Real import succeeded; use the LENS check.
    from lens.checks.tabpfn_anomaly import TabPFNAnomalyCheck

    check = TabPFNAnomalyCheck(
        field="balance",
        context_window=60,
        score_threshold=2.0,
        min_history=20,
    )
    result = check.run(lf)
    out: dict[int, float] = {}
    base = date(2024, 1, 1)
    for issue in result.issues:
        idx = (issue.snapshot_date - base).days
        score = abs(float(issue.details.get("score", 0.0)))
        out[idx] = score
    return out


# ---------------------------------------------------------------------------
# Ensemble
# ---------------------------------------------------------------------------


def _ensemble(*detector_outputs: dict[int, float]) -> dict[int, tuple[int, float]]:
    """Score-averaged / vote ensemble.

    For each candidate index in the union of flagged indices, count how many
    detectors flagged it. Keep indices with vote >= 2. Returned mapping:
    ``{index: (vote_count, max_score_across_detectors)}``. Top-1 selection
    uses vote_count then max_score as tiebreaker.
    """
    candidates: set[int] = set()
    for d in detector_outputs:
        candidates.update(d.keys())

    out: dict[int, tuple[int, float]] = {}
    for idx in candidates:
        votes = sum(1 for d in detector_outputs if idx in d)
        if votes < 2:
            continue
        max_score = max(d[idx] for d in detector_outputs if idx in d)
        out[idx] = (votes, max_score)
    return out


def _top1_recall(scores: dict, anomaly_idx: int, *, ensemble: bool = False) -> int:
    """Returns 1 if the top-1 ranked index by the detector matches
    ``anomaly_idx``, else 0. Empty score map → 0 (the detector flagged
    nothing).

    For single detectors, scores is ``{idx: float}`` — rank by score desc.
    For the ensemble, scores is ``{idx: (votes, max_score)}`` — rank by
    (votes desc, max_score desc).
    """
    if not scores:
        return 0
    if ensemble:
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))
    else:
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top_idx = ranked[0][0]
    return 1 if top_idx == anomaly_idx else 0


# ---------------------------------------------------------------------------
# Metrics helper used by both tests
# ---------------------------------------------------------------------------


def _compute_metrics() -> dict[str, dict[str, int]]:
    """Run every detector + ensemble on every fixture; return recall@1 grid.

    Shape::

        {
          "event_driven": {"stl": 0, "rolling_z": 1, "tabpfn": <0|1|None>, "ensemble": 1},
          ...
        }

    A ``None`` for ``tabpfn`` means the optional dep wasn't installed; the
    ensemble in that case uses only STL + rolling-Z.
    """
    grid: dict[str, dict[str, int]] = {}
    for name in FIXTURES:
        lf, anomaly_idx, _snapshots = _load_fixture(name)
        period = STL_PERIOD[name]

        stl = _stl_scores(lf, period=period)
        rz = _rolling_z_scores(lf, window=ROLLING_WINDOW)
        tabpfn = _tabpfn_scores(lf)

        if tabpfn is not None:
            ens = _ensemble(stl, rz, tabpfn)
        else:
            ens = _ensemble(stl, rz)

        row: dict[str, int] = {
            "stl": _top1_recall(stl, anomaly_idx),
            "rolling_z": _top1_recall(rz, anomaly_idx),
            "ensemble": _top1_recall(ens, anomaly_idx, ensemble=True),
        }
        if tabpfn is not None:
            row["tabpfn"] = _top1_recall(tabpfn, anomaly_idx)
        else:
            row["tabpfn"] = -1  # sentinel meaning "skipped (not installed)"
        grid[name] = row
    return grid


def _format_grid(grid: dict[str, dict[str, int]]) -> str:
    lines = []
    header = f"{'fixture':<16}{'stl':>6}{'rolling_z':>12}{'tabpfn':>10}{'ensemble':>12}"
    lines.append(header)
    lines.append("-" * len(header))
    for name in FIXTURES:
        row = grid[name]
        tabpfn_cell = "n/a" if row["tabpfn"] == -1 else str(row["tabpfn"])
        lines.append(
            f"{name:<16}{row['stl']:>6}{row['rolling_z']:>12}"
            f"{tabpfn_cell:>10}{row['ensemble']:>12}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.eval
def test_per_fixture_metrics_report(capsys: pytest.CaptureFixture[str]) -> None:
    """Always passes. Prints a per-fixture, per-detector recall@1 table so
    the eval doubles as a metrics dashboard when run with ``pytest -v``."""
    grid = _compute_metrics()
    table = _format_grid(grid)
    # Use print so pytest -v / -s shows it; also log for completeness.
    print("\nT13 TS-ensemble eval — recall@1 by fixture and detector\n")
    print(table)
    logger.info("T13 metrics grid:\n%s", table)
    assert grid  # trivially true; this test is a reporter


@pytest.mark.eval
def test_ensemble_strictly_better_on_at_least_one_fixture(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Strict-better criterion (eng review).

    For every fixture: ensemble recall@1 must be >= max(individual recall@1).
    Across the 3 fixtures: ensemble recall@1 must be strictly > max
    individual on at least 1.

    If this fails, the recommendation is documented in the failure message:
    ship MVP with detector-routing rather than score-average ensembling, and
    defer ensembling to v2.1.
    """
    grid = _compute_metrics()

    strictly_better_fixtures: list[str] = []
    regressions: list[str] = []
    for name in FIXTURES:
        row = grid[name]
        individuals = [row["stl"], row["rolling_z"]]
        if row["tabpfn"] != -1:
            individuals.append(row["tabpfn"])
        max_indiv = max(individuals)
        ens = row["ensemble"]
        if ens < max_indiv:
            regressions.append(
                f"{name}: ensemble={ens} < max_individual={max_indiv} "
                f"(individuals={individuals})"
            )
        elif ens > max_indiv:
            strictly_better_fixtures.append(name)

    table = _format_grid(grid)
    print("\nT13 strict-better criterion — recall@1 grid:\n")
    print(table)

    if regressions:
        pytest.fail(
            "Ensemble REGRESSED below the best single detector on >=1 fixture. "
            "Recommendation: ship MVP with detector-routing (one detector per "
            "data shape), defer ensembling to v2.1.\n"
            + "\n".join(regressions)
            + "\n\nFull grid:\n"
            + table
        )

    if not strictly_better_fixtures:
        pytest.fail(
            "Ensemble TIED the best single detector on every fixture (no strict "
            "win). Score-averaged ensembling provides no measurable lift. "
            "Recommendation: ship MVP with detector-routing (one detector per "
            "data shape — e.g. rolling-Z for event-driven, STL for seasonal, "
            "TabPFN for mixed when available), defer ensembling to v2.1.\n\n"
            "Full grid:\n" + table
        )

    logger.info(
        "Ensemble strictly better on: %s", ", ".join(strictly_better_fixtures)
    )
