{{
  config(
    materialized='incremental',
    tags=['daily']
  )
}}

select
    event_id,
    user_id,
    event_name,
    event_ts
from {{ source('raw', 'events') }}
