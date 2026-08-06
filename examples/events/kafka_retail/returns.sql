-- ksqlDB returns pipeline
CREATE STREAM returns_src (return_id VARCHAR, store_id BIGINT, refund DOUBLE, event_ts BIGINT)
  WITH (KAFKA_TOPIC='returns.v2', VALUE_FORMAT='AVRO', TIMESTAMP='event_ts');

CREATE TABLE returns_per_store
  WITH (KAFKA_TOPIC='returns.hourly', VALUE_FORMAT='AVRO')
  AS SELECT store_id, COUNT(*) AS return_count, SUM(refund) AS total_refund
     FROM returns_src
     WINDOW TUMBLING (SIZE 1 HOURS)
     GROUP BY store_id
     EMIT CHANGES;
