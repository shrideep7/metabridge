CREATE TABLE dbo.orders (
  order_id INT, customer_id INT, amount DECIMAL(12,2),
  status NVARCHAR(20), order_dt DATETIME
);

CREATE VIEW dbo.v_recent_orders AS
SELECT TOP 1000 order_id, customer_id,
       ISNULL(amount, 0) AS amount,
       DATEDIFF(day, order_dt, GETDATE()) AS age_days
FROM dbo.orders
WHERE status <> N'CANCELLED';
GO
