-- Pattern: PostgreSQL named WINDOW projection
-- Expected: ["customer_id", "order_id", "order_rank", "running_amount"]
SELECT
    customer_id,
    order_id,
    ROW_NUMBER() OVER customer_orders AS order_rank,
    SUM(amount) OVER customer_orders AS running_amount
FROM orders
WINDOW customer_orders AS (PARTITION BY customer_id ORDER BY ordered_at);