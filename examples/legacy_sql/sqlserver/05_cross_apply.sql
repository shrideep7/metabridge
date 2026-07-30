CREATE VIEW dbo.v_latest_order AS
SELECT c.customer_id, c.name, o.order_id, o.amount
FROM dbo.customers c
CROSS APPLY (
  SELECT TOP 1 order_id, amount FROM dbo.orders o
  WHERE o.customer_id = c.customer_id ORDER BY o.order_dt DESC
) o;
GO
