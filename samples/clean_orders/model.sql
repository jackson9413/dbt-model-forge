{{
  config(
    materialized='incremental',
    unique_key='order_id',
    tags=['daily', 'finance']
  )
}}

with stg_orders as (

    select
        id as order_id,
        customer_id,
        order_total,
        order_status,
        order_updated_at as updated_at
    from {{ source('raw', 'orders') }}
    where order_status != 'cancelled'

),

int_orders as (

    select
        order_id,
        customer_id,
        order_total,
        order_status,
        updated_at
    from stg_orders

)

select
    order_id,
    customer_id,
    order_total,
    order_status,
    updated_at
from int_orders

{% if is_incremental() %}
    where updated_at > (select max(updated_at) from {{ this }})
{% endif %}
