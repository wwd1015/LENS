-- Northwind Capital data pipeline — senior_debt model.
--
-- Stands in for the customer's real data-pipeline repo (the producing code
-- LENS investigates). The lineage page senior-debt.lineage.md points here and
-- declares the recent change that introduced the Q2 advance-rate override —
-- the planted bug the RCA agent traces back to.
--
-- The senior tranche balance is the pool balance times the deal advance rate,
-- EXCEPT for a hand-entered Q2 true-up override that hard-codes 0.84 for one
-- deal on one snapshot. That override is the defect.
CREATE OR REPLACE TABLE senior_debt AS
SELECT
    lp.deal_id,
    lp.snapshot_date,
    SUM(lp.balance) * (
        CASE
            -- TICKET-4821 Q2 true-up: temporary advance-rate override.
            -- Should have been reverted before the 2026-06-30 close.
            WHEN lp.deal_id = 'Sterling Mid-Market Fund II'
                 AND lp.snapshot_date = DATE '2026-06-30'
            THEN 0.84
            ELSE dt.advance_rate
        END
    ) AS balance
FROM loan_pool lp
JOIN deal_terms dt
  ON dt.deal_id = lp.deal_id
 AND dt.snapshot_date = lp.snapshot_date
GROUP BY lp.deal_id, lp.snapshot_date, dt.advance_rate;
