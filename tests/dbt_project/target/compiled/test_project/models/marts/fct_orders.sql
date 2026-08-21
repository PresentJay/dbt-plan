

SELECT
    order_id,
    store_id,
    customer_uuid,
    order_date,
    'unknown' AS source
FROM "memory"."main"."stg_orders"