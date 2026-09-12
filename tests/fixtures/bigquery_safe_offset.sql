-- Pattern: BigQuery SAFE_OFFSET array projection
-- Expected: ["order_id", "first_book_id"]
SELECT order_id, book_ids[SAFE_OFFSET(0)] AS first_book_id FROM orders;