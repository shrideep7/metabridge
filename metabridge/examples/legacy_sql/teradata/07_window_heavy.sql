CREATE VIEW v_account_trends AS
SELECT account_id, txn_ts, amount,
       ZEROIFNULL(LAG(amount) OVER (PARTITION BY account_id ORDER BY txn_ts)) AS prev_amount,
       SUM(amount) OVER (PARTITION BY account_id ORDER BY txn_ts
                         ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS rolling_7,
       RANK() OVER (PARTITION BY account_id ORDER BY amount DESC) AS amt_rank
FROM dw.txn_history
QUALIFY amt_rank <= 100;
