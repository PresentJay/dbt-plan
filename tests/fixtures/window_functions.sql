-- Window functions: common in dbt incremental models
SELECT
    user_id,
    order_date,
    channel,
    ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY order_date DESC) AS row_num,
    LAG(order_date) OVER (PARTITION BY user_id ORDER BY order_date) AS prev_order_date,
    SUM(revenue) OVER (PARTITION BY user_id) AS lifetime_spend
FROM events
QUALIFY row_num = 1
