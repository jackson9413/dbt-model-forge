select
    o.*,
    c.customer_name,
    p.product_name
from analytics.raw.orders o
left join analytics.raw.customers c on o.customer_id = c.customer_id
left join analytics.raw.products p on o.product_id = p.product_id
where o.order_date >= '2024-01-01'
limit 1000
