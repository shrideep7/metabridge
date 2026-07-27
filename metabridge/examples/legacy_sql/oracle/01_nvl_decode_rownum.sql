-- customer snapshot with legacy expressions
CREATE TABLE stg_customers (
  cust_id NUMBER(10), name VARCHAR2(100), status VARCHAR2(10),
  credit_limit NUMBER(12,2), created_dt DATE
);

CREATE OR REPLACE VIEW v_active_customers AS
SELECT cust_id,
       NVL(name, 'UNKNOWN') AS name,
       DECODE(status, 'A', 'ACTIVE', 'S', 'SUSPENDED', 'INACTIVE') AS status_desc,
       NVL(credit_limit, 0) AS credit_limit,
       SYSDATE AS loaded_at
FROM stg_customers
WHERE status IS NOT NULL AND ROWNUM <= 10000;
