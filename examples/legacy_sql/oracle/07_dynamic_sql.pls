CREATE OR REPLACE PROCEDURE archive_partition(p_table IN VARCHAR2) IS
  v_sql VARCHAR2(4000);
BEGIN
  v_sql := 'INSERT INTO ' || p_table || '_archive SELECT * FROM ' || p_table;
  EXECUTE IMMEDIATE v_sql;
  EXECUTE IMMEDIATE 'TRUNCATE TABLE ' || p_table;
END archive_partition;
/
