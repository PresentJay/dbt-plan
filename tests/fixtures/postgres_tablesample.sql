-- Pattern: PostgreSQL TABLESAMPLE SYSTEM with REPEATABLE seed
-- Expected: ["order_id", "customer_id"]
SELECT order_id, customer_id FROM orders TABLESAMPLE SYSTEM (10) REPEATABLE (7);