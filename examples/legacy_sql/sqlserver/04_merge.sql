MERGE dbo.dim_customer AS t
USING (SELECT customer_id, name, email FROM dbo.stg_customer) AS s
ON t.customer_id = s.customer_id
WHEN MATCHED THEN UPDATE SET t.name = s.name, t.email = s.email
WHEN NOT MATCHED THEN INSERT (customer_id, name, email)
     VALUES (s.customer_id, s.name, s.email);
GO
