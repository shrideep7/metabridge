CREATE TABLE employees (
  emp_id NUMBER(10), mgr_id NUMBER(10), ename VARCHAR2(80), deptno NUMBER(4)
);

CREATE OR REPLACE VIEW v_org_chart AS
SELECT emp_id, ename, mgr_id, LEVEL
FROM employees
START WITH mgr_id IS NULL
CONNECT BY PRIOR emp_id = mgr_id;
