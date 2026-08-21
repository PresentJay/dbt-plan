

SELECT
    store_id,
    'Sample Title' AS title
FROM "memory"."main"."stg_orders"
GROUP BY 1