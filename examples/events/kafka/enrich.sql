-- ksqlDB enrichment + windowed aggregate
CREATE STREAM orders_src (order_id VARCHAR, customer_id VARCHAR, amount DOUBLE, event_ts BIGINT)
  WITH (KAFKA_TOPIC='orders.v1', VALUE_FORMAT='AVRO');

CREATE TABLE orders_per_customer
  WITH (KAFKA_TOPIC='orders.enriched', VALUE_FORMAT='AVRO')
  AS SELECT customer_id, COUNT(*) AS order_count, SUM(amount) AS total_amount
     FROM orders_src
     WINDOW TUMBLING (SIZE 5 MINUTES)
     GROUP BY customer_id
     EMIT CHANGES;
