-- Pattern: Snowflake COUNT_IF conditional aggregate projection
-- Expected: ["customer_id", "paid_orders"]
SELECT customer_id, COUNT_IF(status = 'paid') AS paid_orders FROM orders GROUP BY customer_id;