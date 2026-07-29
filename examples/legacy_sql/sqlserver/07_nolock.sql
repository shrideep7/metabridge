CREATE VIEW dbo.v_dashboard AS
SELECT status, COUNT(*) AS cnt, SUM(amount) AS total
FROM dbo.orders WITH (NOLOCK)
GROUP BY status;
GO
