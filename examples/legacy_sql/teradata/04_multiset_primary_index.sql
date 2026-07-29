CREATE MULTISET TABLE dw.txn_history (
  txn_id BIGINT, account_id INTEGER, amount DECIMAL(14,2),
  txn_ts TIMESTAMP
) PRIMARY INDEX (account_id);
