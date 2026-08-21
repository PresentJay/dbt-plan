{{ config(materialized='table') }}

SELECT
    store_id,
    'Sample Title' AS title
FROM {{ ref('stg_orders') }}
GROUP BY 1
