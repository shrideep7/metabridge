{{ config(materialized='incremental', unique_key='order_id', incremental_strategy='merge') }}

select
    order_id,
    customer_id,
    order_date,
    upper(order_status) as order_status,
    cast(amount as decimal(18,2)) as amount,
    updated_at
from {{ source('raw', 'raw_orders') }}
where order_status is not null

{% if is_incremental() %}
  and updated_at > (select max(updated_at) from {{ this }})
{% endif %}
