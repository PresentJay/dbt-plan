-- Pattern: PostgreSQL ordered-set aggregate projection
-- Expected: ["customer_id", "median_amount"]
SELECT
    customer_id,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) AS median_amount
FROM orders
GROUP BY customer_id;