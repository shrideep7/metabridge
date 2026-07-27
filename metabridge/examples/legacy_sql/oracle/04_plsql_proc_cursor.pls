CREATE OR REPLACE PROCEDURE refresh_sales_summary(
  p_region IN VARCHAR2,
  p_rows OUT NUMBER
) IS
  v_total NUMBER := 0;
  CURSOR c_stores IS SELECT store_id FROM stores WHERE region = p_region;
BEGIN
  INSERT INTO etl_audit_log (proc_name, run_at)
  VALUES ('refresh_sales_summary', SYSDATE);

  DELETE FROM sales_summary WHERE region = p_region;

  INSERT INTO sales_summary (region, store_id, total_amount)
  SELECT region, store_id, SUM(amount)
  FROM sales
  WHERE region = p_region
  GROUP BY region, store_id;

  COMMIT;
EXCEPTION
  WHEN OTHERS THEN
    ROLLBACK;
    RAISE;
END refresh_sales_summary;
/
