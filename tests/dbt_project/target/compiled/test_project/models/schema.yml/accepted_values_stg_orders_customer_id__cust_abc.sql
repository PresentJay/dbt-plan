
    
    

with all_values as (

    select
        customer_id as value_field,
        count(*) as n_records

    from "memory"."main"."stg_orders"
    group by customer_id

)

select *
from all_values
where value_field not in (
    'cust_abc'
)


