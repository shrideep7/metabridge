CREATE TABLE retail_sales (
  store_id INTEGER, sale_dt DATE, amount DECIMAL(12,2)
);

CREATE VIEW v_top_stores AS
SELECT store_id, SUM(amount) AS total,
       ROW_NUMBER() OVER (ORDER BY SUM(amount) DESC) AS rnk
FROM retail_sales
GROUP BY store_id
QUALIFY rnk <= 10;
