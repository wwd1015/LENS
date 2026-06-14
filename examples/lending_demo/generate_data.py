"""Regenerate the synthetic lending CSVs for the demo. Deterministic — no RNG.

Three real-sounding deals, 18 monthly snapshots. The data is internally
consistent (senior_debt.balance == sum(loan_pool.balance per deal) *
advance_rate) except for two planted anomalies on the final snapshot:

* the "Sterling Mid-Market Fund II" senior-debt balance is inflated 12% —
  breaches the ``senior-debt-equals-pool-x-advance-rate`` wiki rule AND spikes
  the ``stl_residual`` series, so the two detector families agree and the
  orchestrator's agreement boost kicks in.
* two of "Granite Peak Direct Lending"'s borrowers have a null ``status`` —
  fires ``null_check``.

Run from the repo root::

    python examples/lending_demo/generate_data.py
"""

from __future__ import annotations

import calendar
import csv
from datetime import date
from pathlib import Path

OUT_DIR = Path(__file__).parent / "data"

# Deal name → (advance_rate, {borrower: starting_balance}). Borrower names
# stand in for loan_id; deal names stand in for deal_id. No commas (CSV-safe).
DEALS = {
    "Brightwater CLO 2024-1": (
        0.80,
        {"Cedar Park Health": 1_200_000.0, "Vega Robotics": 800_000.0, "Atlas Freight": 500_000.0},
    ),
    "Sterling Mid-Market Fund II": (
        0.75,
        {"Northwind Hospitality": 2_000_000.0, "Lumen Diagnostics": 1_500_000.0},
    ),
    "Granite Peak Direct Lending": (
        0.70,
        {
            "Redwood Materials": 900_000.0,
            "Ironclad Security": 700_000.0,
            "Harbor Point Foods": 400_000.0,
        },
    ),
}

# 1% of original balance amortizes each month.
AMORT_RATE = 0.01

N_SNAPSHOTS = 18
FIRST_YEAR, FIRST_MONTH = 2025, 1  # first snapshot: 2025-01-31

# Planted breach: this deal's senior-debt is inflated 12% on the last snapshot.
INFLATED_DEAL = "Sterling Mid-Market Fund II"
SENIOR_DEBT_INFLATION = 1.12
# Planted nulls: these borrowers lose their status on the last snapshot.
NULL_STATUS_LOANS = {"Redwood Materials", "Ironclad Security"}


def _snapshot_dates() -> list[date]:
    dates = []
    year, month = FIRST_YEAR, FIRST_MONTH
    for _ in range(N_SNAPSHOTS):
        dates.append(date(year, month, calendar.monthrange(year, month)[1]))
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return dates


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    snapshots = _snapshot_dates()
    last = snapshots[-1]

    loan_pool_rows = []
    senior_debt_rows = []
    deal_terms_rows = []

    for snap_idx, snap in enumerate(snapshots):
        for deal_id, (advance_rate, loans) in DEALS.items():
            pool_total = 0.0
            for loan_id, start_balance in loans.items():
                balance = round(start_balance * (1.0 - AMORT_RATE * snap_idx), 2)
                pool_total += balance
                status = "performing"
                if snap == last and loan_id in NULL_STATUS_LOANS:
                    status = ""  # null in CSV
                loan_pool_rows.append(
                    {
                        "deal_id": deal_id,
                        "loan_id": loan_id,
                        "snapshot_date": snap.isoformat(),
                        "balance": balance,
                        "status": status,
                    }
                )

            senior_balance = round(pool_total * advance_rate, 2)
            if snap == last and deal_id == INFLATED_DEAL:
                senior_balance = round(senior_balance * SENIOR_DEBT_INFLATION, 2)
            senior_debt_rows.append(
                {
                    "deal_id": deal_id,
                    "snapshot_date": snap.isoformat(),
                    "balance": senior_balance,
                }
            )
            deal_terms_rows.append(
                {
                    "deal_id": deal_id,
                    "snapshot_date": snap.isoformat(),
                    "advance_rate": advance_rate,
                }
            )

    for name, rows in (
        ("loan_pool.csv", loan_pool_rows),
        ("senior_debt.csv", senior_debt_rows),
        ("deal_terms.csv", deal_terms_rows),
    ):
        path = OUT_DIR / name
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {path} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
