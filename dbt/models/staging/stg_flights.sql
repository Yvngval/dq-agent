with source as (
    select * from {{ source('raw', 'flights') }}
)

select
    _row_id,
    md5(icao24 || '|' || cast(firstseen as varchar)) as flight_id,
    icao24 as aircraft_id,
    callsign,
    case when regexp_matches(callsign, '^[A-Z]{3}[0-9]') then left(callsign, 3) end as airline_icao,
    typecode as aircraft_type,
    origin as origin_icao,
    destination as destination_icao,
    firstseen as first_seen_at,
    lastseen as last_seen_at,
    day as flight_date,
    date_diff('minute', firstseen, lastseen) as duration_min,
    latitude_1 as first_latitude,
    longitude_1 as first_longitude,
    altitude_1 as first_altitude_m,
    latitude_2 as last_latitude,
    longitude_2 as last_longitude,
    altitude_2 as last_altitude_m
from source
