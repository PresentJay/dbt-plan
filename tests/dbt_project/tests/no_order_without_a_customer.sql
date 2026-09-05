-- A singular test. The manifest records no column for it, so dbt-plan has to
-- read this file to know that dropping customer_id breaks it.
SELECT order_id FROM {{ ref('stg_orders') }} WHERE customer_id IS NULL
