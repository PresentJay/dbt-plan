-- Pattern: PostgreSQL AT TIME ZONE projection
-- Expected: ["order_id", "ordered_utc"]
SELECT order_id, ordered_at AT TIME ZONE 'UTC' AS ordered_utc FROM orders;