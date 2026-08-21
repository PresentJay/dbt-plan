-- Multi-CTE chain: very common in dbt models
WITH source AS (
    SELECT
        id,
        created_at,
        raw_data
    FROM raw_orders
),

renamed AS (
    SELECT
        id AS order_id,
        created_at AS ordered_at,
        raw_data:user_id::STRING AS user_id,
        raw_data:customer_tier::STRING AS customer_tier
    FROM source
),

final AS (
    SELECT
        order_id,
        ordered_at,
        user_id,
        customer_tier,
        DATE_TRUNC('day', ordered_at) AS order_date
    FROM renamed
)

SELECT * FROM final
