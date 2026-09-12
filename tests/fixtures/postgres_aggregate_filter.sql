-- Pattern: PostgreSQL aggregate FILTER projection
-- Expected: ["customer_id", "paid_orders", "net_amount"]
SELECT
    customer_id,
    COUNT(*) FILTER (WHERE status = 'paid') AS paid_orders,
    SUM(amount) FILTER (WHERE refunded = false) AS net_amount
FROM orders
GROUP BY customer_id;