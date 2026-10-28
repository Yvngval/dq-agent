with source as (
    select * from {{ source('raw', 'readings') }}
)

select
    _row_id,
    reading_id,
    station_code,
    observed_at as observed_at_utc,
    obs_date as observation_date,
    temperature_c as air_temp_c,
    temperature_f as air_temp_f,
    humidity_pct as relative_humidity_pct,
    pressure_hpa as sea_level_pressure_hpa,
    wind_speed_kmh,
    precipitation_mm,
    sensor_model
from source
