SELECT
    store_id,
    order_date,
    shipping_info,
    COUNT(*) AS order_count,
    COUNT(DISTINCT customer_id) AS unique_customers,
    SUM(revenue) AS total_sales
FROM int_order_enriched
GROUP BY 1, 2, 3
