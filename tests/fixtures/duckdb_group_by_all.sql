-- Pattern: DuckDB GROUP BY ALL projection
-- Expected: ["customer_id", "store_id", "total_amount"]
SELECT
    customer_id,
    store_id,
    SUM(amount) AS total_amount
FROM orders
GROUP BY ALL;