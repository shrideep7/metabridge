SELECT order_id, customer_id, amount
INTO #valid
FROM dbo.orders WHERE status = 'OK';
GO
SELECT customer_id, SUM(amount) AS total_amount
INTO #by_customer
FROM #valid GROUP BY customer_id;
GO
CREATE TABLE dbo.customer_totals AS
SELECT customer_id, total_amount FROM #by_customer;
GO
