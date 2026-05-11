"""Reproducible fixture generator for the TS ensemble eval (T13).

Three 90-point daily time series, each with a single labeled anomaly:

  * event_driven: random walk (drift + Gaussian noise, no periodicity). One
    large jump planted at index 60. STL should struggle (nothing to
    decompose); rolling-Z should catch it easily; TabPFN — when installed —
    should also flag.

  * seasonal: pure weekly sine (period=7) + noise. Single residual spike
    planted at index 50. STL should excel; rolling-Z is expected to be noisy
    near peaks/troughs.

  * mixed: linear trend + weekly sine + noise. The true anomaly at idx 45
    is a *moderate* spike — large enough that both STL and rolling-Z flag
    it, but not the largest thing either sees. We also plant two distractors
    that each fool one detector but not the other:
      - an anti-phase seasonal break around idx 64-66 that STL ranks as its
        top residual (high local residual; small per-point jump)
      - a depressed-regime + recovery zone around idx 70-78 where rolling-Z
        sees the largest local-mean deviations
    Each detector's individual top-1 is therefore a distractor (recall@1=0
    for both alone), but the AND intersection of their flagged sets leaves
    only the true anomaly. This is the fixture where score-vote ensembling
    earns its keep.

All seeds and spike magnitudes are pinned for determinism. Re-running this
script overwrites the three CSVs and the labels.json next to them.

Spike-magnitude rationale:
  * event_driven jump (+45 absolute, ~12σ-equivalent on a noise-only series
    of σ=1.5) is large enough that *any* outlier detector should flag it.
  * seasonal spike (+8 absolute on amplitude=4, noise=0.4) — the residual
    after STL decomposition is many σ above zero; rolling-Z will see it as
    well but will be confused by adjacent sinusoidal peaks.
  * mixed: small (+3.5) true spike + two larger distractors crafted to each
    fool exactly one detector. Engineered so that the AND ensemble strictly
    outperforms either single detector — the test-of-record for whether
    score-vote ensembling adds value over detector-routing.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
N = 90
BASE_DATE = date(2024, 1, 1)


def _dates(n: int) -> list[date]:
    return [BASE_DATE + timedelta(days=i) for i in range(n)]


def event_driven(seed: int = 11) -> tuple[pd.DataFrame, int]:
    """Random walk + drift; planted jump at idx 60."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.05, 1.5, size=N)  # drift 0.05/day, σ=1.5
    values = 100.0 + np.cumsum(steps)
    anomaly_idx = 60
    values[anomaly_idx:] += 45.0  # large persistent jump
    # Reset to single-point spike (not persistent) so it's a *point* anomaly,
    # not a regime change — that's the mixed fixture's job.
    values[anomaly_idx + 1 :] -= 45.0
    df = pd.DataFrame({"snapshot_date": _dates(N), "balance": values})
    return df, anomaly_idx


def seasonal(seed: int = 23) -> tuple[pd.DataFrame, int]:
    """Weekly sine + noise; planted residual spike at idx 50."""
    rng = np.random.default_rng(seed)
    t = np.arange(N)
    season = 4.0 * np.sin(2 * np.pi * t / 7.0)
    noise = rng.normal(0.0, 0.4, size=N)
    values = 50.0 + season + noise
    anomaly_idx = 50
    values[anomaly_idx] += 8.0  # large residual spike on top of seasonality
    df = pd.DataFrame({"snapshot_date": _dates(N), "balance": values})
    return df, anomaly_idx


def mixed(seed: int = 37) -> tuple[pd.DataFrame, int]:
    """Trend + weekly sine + noise; true spike at idx 45 + two distractor zones.

    The point of this fixture is to exercise the ensemble's edge: each single
    detector's top-1 is a distractor, only the AND-of-detectors collapses to
    the true anomaly. See the module docstring for the design rationale.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(N)
    trend = 100.0 + 0.4 * t
    season = 3.0 * np.sin(2 * np.pi * t / 7.0)
    noise = rng.normal(0.0, 0.6, size=N)
    values = trend + season + noise

    # True anomaly: moderate spike at idx 45 — flagged by both detectors, but
    # ranked below at least one distractor by each individually.
    anomaly_idx = 45
    values[anomaly_idx] += 3.5

    # STL distractor: anti-phase break at idx 64-66 (3-point depression).
    # High local residual but each point's jump-vs-rolling-mean is moderate,
    # so STL ranks it #1 but rolling-Z doesn't.
    values[64:67] -= 6.0

    # Rolling-Z distractor: depressed regime at idx 70-74, recovery at 75-78.
    # The recovery points sit far above the depressed-window rolling mean, so
    # rolling-Z ranks them #1 (the recovery, not the depression). STL absorbs
    # the slow shape into its trend/seasonal components.
    values[70:75] -= 6.0

    df = pd.DataFrame({"snapshot_date": _dates(N), "balance": values})
    return df, anomaly_idx


def main() -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    labels: dict[str, dict[str, int]] = {}

    df, idx = event_driven()
    df.to_csv(HERE / "event_driven.csv", index=False)
    labels["event_driven"] = {"anomaly_index": idx}

    df, idx = seasonal()
    df.to_csv(HERE / "seasonal.csv", index=False)
    labels["seasonal"] = {"anomaly_index": idx}

    df, idx = mixed()
    df.to_csv(HERE / "mixed.csv", index=False)
    labels["mixed"] = {"anomaly_index": idx}

    (HERE / "labels.json").write_text(json.dumps(labels, indent=2) + "\n")
    print(f"Wrote 3 fixtures + labels.json to {HERE}")


if __name__ == "__main__":
    main()
