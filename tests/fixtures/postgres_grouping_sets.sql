-- Pattern: PostgreSQL GROUPING SETS and GROUPING projection
-- Expected: ["customer_id", "store_id", "total_amount", "grouping_mask"]
SELECT
    customer_id,
    store_id,
    SUM(amount) AS total_amount,
    GROUPING(customer_id, store_id) AS grouping_mask
FROM orders
GROUP BY GROUPING SETS ((customer_id, store_id), (customer_id), ());