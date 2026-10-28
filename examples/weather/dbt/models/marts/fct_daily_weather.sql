-- Synthèse journalière par station : températures extrêmes, cumul de pluie, nombre de relevés.
-- SQL portable : le même projet tourne sur DuckDB et sur PostgreSQL.
select
    r.station_code,
    s.country_code,
    r.observation_date,
    min(r.air_temp_c) as min_temp_c,
    max(r.air_temp_c) as max_temp_c,
    round(cast(avg(r.relative_humidity_pct) as numeric), 1) as avg_humidity_pct,
    round(cast(sum(r.precipitation_mm) as numeric), 1) as precipitation_mm,
    count(*) as readings
from {{ ref('stg_readings') }} as r
inner join {{ ref('stg_stations') }} as s using (station_code)
group by r.station_code, s.country_code, r.observation_date
