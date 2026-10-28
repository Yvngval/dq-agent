with source as (
    select * from {{ source('raw', 'stations') }}
)

select
    _row_id,
    station_code,
    station_name,
    country as country_code,
    latitude,
    longitude,
    elevation_m
from source
