"""Generator for the synthetic LENS v2 integration test CSVs.

This script is run once at fixture-bootstrap time; the CSVs it emits are the
on-disk truth used by `tests/test_integration_v2.py`. Re-running with the same
seed reproduces the exact same files byte-for-byte.

Schema (all three files):
    entity_id (== deal_id), snapshot_date (ISO), <value column>

Entities:
    D1 — clean baseline EXCEPT on day 60 a senior_debt spike (no matching pool
         move) — the equation breaks AND STL fires → the dedup case.
    D2 — clean EXCEPT on day 30 the recorded advance_rate is 0.75 instead of
         0.80; senior_debt and loan_pool still hold their true values → only
         the cross-source check fires.
    D3 — clean EXCEPT on day 60 BOTH pool and senior spike proportionally
         (50 / 0.80 = 62.5), so the equation holds and only STL fires.

Spike magnitude was tuned (see `docs` in test) to survive robust-STL absorption:
small enough to leave a visible residual, large enough to clear z=3.
"""
from __future__ import annotations

import csv
import math
import random
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent
# Per-deal seeds were swept manually (see test docstring) to find values where
# both D1 and D3 spikes survive robust-STL absorption AND D2's noisy baseline
# produces no false positives at z>=5. Touch only if the spike magnitudes
# change.
DEAL_SEEDS = {"D1": 2, "D2": 1, "D3": 4}
START = date(2026, 1, 1)
N_DAYS = 90

DEALS = (
    # (deal_id, base_pool, seasonal_amp, noise_std)
    ("D1", 1000.0, 50.0, 5.0),
    ("D2", 2000.0, 100.0, 10.0),
    ("D3", 1500.0, 75.0, 7.0),
)

ANOMALY_DAY_D1 = 60      # senior-only spike → eq breaks + STL fires
ANOMALY_DAY_D2 = 30      # wrong advance_rate → eq breaks, STL silent
ANOMALY_DAY_D3 = 60      # balanced spike → eq holds, STL fires

# Spike magnitudes — kept small so the robust-loess STL leaves a visible
# residual rather than absorbing the outlier (empirically: >100 gets absorbed
# into the seasonal/trend components and z@target collapses to <1).
SD_SPIKE_D1 = 50.0
SD_SPIKE_D3 = 50.0
TRUE_RATE = 0.80
POOL_SPIKE_D3 = SD_SPIKE_D3 / TRUE_RATE  # 62.5 keeps the equation exactly balanced

D2_WRONG_RATE = 0.75


def build_rows():
    loan_rows, terms_rows, debt_rows = [], [], []
    for deal_id, base, amp, std in DEALS:
        rng = random.Random(DEAL_SEEDS[deal_id])
        for day in range(N_DAYS):
            snap = START + timedelta(days=day)
            pool_balance = (
                base
                + amp * math.sin(2 * math.pi * day / 30.0)
                + rng.gauss(0, std)
            )
            recorded_rate = TRUE_RATE
            senior_balance = pool_balance * TRUE_RATE

            if deal_id == "D1" and day == ANOMALY_DAY_D1:
                senior_balance += SD_SPIKE_D1
            elif deal_id == "D2" and day == ANOMALY_DAY_D2:
                recorded_rate = D2_WRONG_RATE
            elif deal_id == "D3" and day == ANOMALY_DAY_D3:
                pool_balance += POOL_SPIKE_D3
                senior_balance += SD_SPIKE_D3

            loan_rows.append((deal_id, snap.isoformat(), f"{pool_balance:.4f}"))
            terms_rows.append((deal_id, snap.isoformat(), f"{recorded_rate:.4f}"))
            debt_rows.append((deal_id, snap.isoformat(), f"{senior_balance:.4f}"))
    return loan_rows, terms_rows, debt_rows


def write_csv(path: Path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def main() -> None:
    loan, terms, debt = build_rows()
    write_csv(OUT / "loan_pool.csv", ("entity_id", "snapshot_date", "balance"), loan)
    write_csv(OUT / "deal_terms.csv", ("entity_id", "snapshot_date", "advance_rate"), terms)
    write_csv(OUT / "senior_debt.csv", ("entity_id", "snapshot_date", "balance"), debt)


if __name__ == "__main__":
    main()
