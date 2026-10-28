select
    ident as airport_icao,
    type as airport_type,
    name as airport_name,
    iso_country as country_code,
    municipality,
    latitude_deg as latitude,
    longitude_deg as longitude,
    elevation_ft
from {{ source('raw', 'airports') }}
