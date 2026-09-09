-- Pattern: BigQuery UNNEST WITH OFFSET projection
-- Expected: ["order_id", "item_sku", "item_offset"]
SELECT
    o.order_id,
    item.sku AS item_sku,
    item_offset
FROM orders AS o
CROSS JOIN UNNEST(o.items) AS item WITH OFFSET AS item_offset;