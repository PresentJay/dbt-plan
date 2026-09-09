-- Pattern: DuckDB SEMI JOIN projection (left columns only)
-- Expected: ["customer_id", "customer_name"]
SELECT c.customer_id, c.customer_name FROM customers AS c SEMI JOIN orders AS o ON c.customer_id = o.customer_id;