-- UNION ALL staging pattern: common for multi-source models
SELECT
    'ebook' AS book_format,
    customer_id,
    order_status,
    ordered_at
FROM raw_web_orders

UNION ALL

SELECT
    'print' AS book_format,
    customer_id,
    order_status,
    ordered_at
FROM raw_store_orders
