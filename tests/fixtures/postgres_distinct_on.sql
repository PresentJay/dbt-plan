-- Pattern: PostgreSQL DISTINCT ON projection
-- Expected: ["customer_id", "latest_order_id"]
SELECT DISTINCT ON (customer_id)
    customer_id,
    order_id AS latest_order_id
FROM orders
ORDER BY customer_id, ordered_at DESC;