select
    id as customer_id,
    upper(trim(first_name)) as first_name,
    upper(trim(last_name)) as last_name,
    lower(email) as email,
    case status when 'A' then 'ACTIVE' when 'I' then 'INACTIVE' else 'UNKNOWN' end as status_desc,
    created_at
from {{ source('raw', 'raw_customers') }}
where email is not null
