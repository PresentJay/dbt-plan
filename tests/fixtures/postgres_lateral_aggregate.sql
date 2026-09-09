-- Pattern: PostgreSQL correlated LATERAL aggregate projection
-- Expected: ["customer_id", "last_order_at"]
SELECT c.customer_id, recent.last_order_at FROM customers AS c LEFT JOIN LATERAL (SELECT MAX(o.ordered_at) AS last_order_at FROM orders AS o WHERE o.customer_id = c.customer_id) AS recent ON TRUE;