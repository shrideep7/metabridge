with valid_orders as (
    select
        order_id,
        customer_id,
        order_date,
        amount
    from {{ ref('stg_orders') }}
    where order_status in ('COMPLETED', 'SHIPPED')
)

select
    order_date,
    count(order_id) as order_count,
    sum(amount) as total_revenue,
    avg(amount) as avg_order_value
from valid_orders
group by order_date
