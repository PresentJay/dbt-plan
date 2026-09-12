-- Pattern: Trino map subscript projection
-- Expected: ["order_id", "sales_channel"]
SELECT order_id, attributes['channel'] AS sales_channel FROM orders;