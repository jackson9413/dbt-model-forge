{{
  config(
    materialized='view'
  )
}}

select
    c.customer_id,
    c.customer_name,
    sum(o.order_total) as order,
    avg(o.order_total) as avg_order_value,
    count(o.order_id) as order_count
from analytics_sales.dbo.customers c
join analytics_sales.dbo.orders o on o.customer_id = c.customer_id
where o.order_date >= '2023-01-01'
group by
    c.customer_id,
    c.customer_name
having sum(o.order_total) > 500
