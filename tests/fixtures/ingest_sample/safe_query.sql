-- Test fixture: a benign SQL file with no secrets.
-- Used by tests/test_wiki_ingest.py to verify is_safe_to_send accepts normal
-- production SQL.

SELECT
    deal_id,
    snapshot_date,
    SUM(balance) AS pool_balance
FROM
    loan_pool
WHERE
    status NOT IN ('charged_off', 'paid_off')
GROUP BY
    deal_id, snapshot_date;
