-- Production transformation for senior_debt balance per deal per snapshot.
--
-- The rule LENS should extract from this code is:
--   senior_debt.balance == SUM(loan_pool.balance GROUP BY deal_id)
--                          * deal_terms.advance_rate
-- with a small relative tolerance.

WITH pool_totals AS (
    SELECT
        deal_id,
        snapshot_date,
        SUM(balance) AS pool_balance
    FROM
        loan_pool
    WHERE
        status NOT IN ('charged_off', 'paid_off')
    GROUP BY
        deal_id,
        snapshot_date
),
deal_advance AS (
    SELECT
        deal_id,
        snapshot_date,
        advance_rate
    FROM
        deal_terms
)

INSERT INTO senior_debt (deal_id, snapshot_date, balance)
SELECT
    p.deal_id,
    p.snapshot_date,
    p.pool_balance * d.advance_rate AS balance
FROM
    pool_totals p
    INNER JOIN deal_advance d
        ON p.deal_id = d.deal_id
        AND p.snapshot_date = d.snapshot_date;
