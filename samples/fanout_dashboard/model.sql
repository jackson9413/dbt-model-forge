{{
  config(
    materialized='table',
    tags=['daily']
  )
}}

select
    c.customer_id,
    c.customer_name,
    count(*) as order_count,
    sum(o.order_total) as lifetime_value,
    max(o.order_date) as last_order_date
from {{ ref('stg_customers') }} c
join {{ ref('fct_orders') }} o on o.customer_id = c.customer_id
join {{ ref('fct_order_items') }} i on i.order_id = o.order_id
group by
    c.customer_id,
    c.customer_name
