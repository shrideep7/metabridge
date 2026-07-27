CREATE MACRO dw.load_daily_snapshot (snap_date DATE) AS (
  INSERT INTO dw.account_snapshot
  SELECT account_id, balance, :snap_date FROM dw.accounts;
);
