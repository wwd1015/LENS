-- Toy producing code for the demo's senior_debt table. The lineage page
-- points here so the RCA agent has a real path to `git log` when
-- investigating senior-debt anomalies.
CREATE OR REPLACE TABLE senior_debt AS
SELECT
    lp.deal_id,
    lp.snapshot_date,
    SUM(lp.balance) * dt.advance_rate AS balance
FROM loan_pool lp
JOIN deal_terms dt
  ON dt.deal_id = lp.deal_id
 AND dt.snapshot_date = lp.snapshot_date
GROUP BY lp.deal_id, lp.snapshot_date, dt.advance_rate;
