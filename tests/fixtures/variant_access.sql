-- Pattern: Snowflake VARIANT column access + QUALIFY
-- Expected: ["loaded_at", "shipping_postal_code", "shipping_recipient", "shipping_country", "store_id", "order_date"]

SELECT
    e.loaded_at,
    e.shipping_info:postalCode::STRING AS shipping_postal_code,
    e.shipping_info:recipient::STRING AS shipping_recipient,
    e.shipping_info:country::STRING AS shipping_country,
    e.store_id,
    e.order_date
FROM all_orders AS e
QUALIFY ROW_NUMBER() OVER (PARTITION BY record_uuid ORDER BY ingested_at DESC) = 1
