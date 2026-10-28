-- Trafic journalier des aéroports français : départs, arrivées, mouvements.
with movements as (
    select origin_icao as airport_icao, flight_date, 'departure' as movement
    from {{ ref('fct_flights') }}
    where origin_country = 'FR'
    union all
    select destination_icao as airport_icao, flight_date, 'arrival' as movement
    from {{ ref('fct_flights') }}
    where destination_country = 'FR'
)

select
    m.airport_icao,
    a.airport_name,
    m.flight_date,
    count(*) filter (where m.movement = 'departure') as departures,
    count(*) filter (where m.movement = 'arrival') as arrivals,
    count(*) as movements
from movements as m
inner join {{ ref('stg_airports') }} as a using (airport_icao)
group by all
