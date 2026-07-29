-- Deliberately uses a window function: exercises the SQL-override fallback path.
select
    customer_id,
    lifetime_value,
    rank() over (order by lifetime_value desc) as customer_rank
from {{ ref('customer_orders') }}
