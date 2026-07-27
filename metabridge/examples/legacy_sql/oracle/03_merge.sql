MERGE INTO dim_product t
USING (SELECT product_id, name, list_price FROM stg_products) s
ON (t.product_id = s.product_id)
WHEN MATCHED THEN UPDATE SET t.name = s.name, t.list_price = s.list_price
WHEN NOT MATCHED THEN INSERT (product_id, name, list_price)
     VALUES (s.product_id, s.name, s.list_price);
