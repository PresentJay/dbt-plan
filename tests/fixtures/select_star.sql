-- Pattern: CTE chain ending in SELECT *
-- Expected: ["*"]

WITH base AS (
    SELECT *
    FROM raw_orders
    WHERE order_date >= '2024-01-01'
),

enriched AS (
    SELECT
        base.*,
        dim.country
    FROM base
    INNER JOIN dim_customers AS dim
        ON base.customer_id = dim.customer_id
)

SELECT * FROM enriched
