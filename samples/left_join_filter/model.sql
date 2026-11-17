{{
  config(
    materialized='view'
  )
}}

select
    o.order_id,
    o.customer_id,
    o.order_total,
    c.customer_name,
    p.product_count
from {{ ref('stg_orders') }} o
left join {{ ref('stg_customers') }} c on o.customer_id = c.customer_id
left join {{ ref('stg_products') }} p on o.product_id = p.product_id
where c.customer_name is not null
