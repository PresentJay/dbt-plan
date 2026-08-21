-- Pattern: CTE chain with explicit final SELECT columns
-- Expected: ["store_id", "order_date", "customer_id", "total_sales", "order_count", "is_active", "day_n"]

WITH source_data AS (
    SELECT
        ordered_at,
        ingested_at,
        store_id,
        customer_id
    FROM raw_orders
    WHERE ingested_at > '2024-01-01'
),

reader_daily_agg AS (
    SELECT
        store_id,
        CAST(ordered_at AS DATE) AS order_date,
        customer_id,
        SUM(revenue) AS total_sales,
        COUNT(*) AS order_count,
        1 AS is_active
    FROM source_data
    GROUP BY 1, 2, 3
)

SELECT
    store_id,
    order_date,
    customer_id,
    total_sales,
    order_count,
    is_active,
    CASE
        WHEN signup_date IS NOT NULL
        THEN DATEDIFF('day', signup_date, order_date)
        ELSE NULL
    END AS day_n
FROM reader_daily_agg
