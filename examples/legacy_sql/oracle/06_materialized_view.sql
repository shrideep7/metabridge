CREATE MATERIALIZED VIEW mv_daily_revenue AS
SELECT TRUNC(order_dt) AS order_day, SUM(amount) AS revenue
FROM orders
GROUP BY TRUNC(order_dt);
