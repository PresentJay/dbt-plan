{{ config(
    materialized='incremental',
    on_schema_change='sync_all_columns'
) }}

SELECT
    order_id,
    store_id,
    customer_uuid,
    order_date,
    'unknown' AS source
FROM {{ ref('stg_orders') }}
