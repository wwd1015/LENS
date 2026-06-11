"""Regenerate the synthetic lending CSVs for the demo. Deterministic — no RNG.

Three deals, 18 monthly snapshots. The data is internally consistent
(senior_debt.balance == sum(loan_pool.balance per deal) * advance_rate)
except for two planted anomalies on the final snapshot:

* deal D2's senior-debt balance is inflated 12% — breaches the
  ``senior-debt-equals-pool-x-advance-rate`` wiki rule AND spikes the
  ``stl_residual`` series, so the two detector families agree and the
  orchestrator's agreement boost kicks in.
* two of deal D3's loans have a null ``status`` — fires ``null_check``.

Run from the repo root::

    python examples/lending_demo/generate_data.py
"""

from __future__ import annotations

import calendar
import csv
from datetime import date
from pathlib import Path

OUT_DIR = Path(__file__).parent / "data"

DEALS = {
    # deal_id: (advance_rate, {loan_id: starting_balance})
    "D1": (0.80, {"L01": 1_200_000.0, "L02": 800_000.0, "L03": 500_000.0}),
    "D2": (0.75, {"L04": 2_000_000.0, "L05": 1_500_000.0}),
    "D3": (0.70, {"L06": 900_000.0, "L07": 700_000.0, "L08": 400_000.0}),
}

# 1% of original balance amortizes each month.
AMORT_RATE = 0.01

N_SNAPSHOTS = 18
FIRST_YEAR, FIRST_MONTH = 2025, 1  # first snapshot: 2025-01-31

SENIOR_DEBT_INFLATION = 1.12  # planted breach on D2's final snapshot
NULL_STATUS_LOANS = {"L06", "L07"}  # planted nulls on D3's final snapshot


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
            if snap == last and deal_id == "D2":
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
