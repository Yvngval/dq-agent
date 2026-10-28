select
    f.*,
    o.country_code as origin_country,
    d.country_code as destination_country,
    case
        when o.country_code is null or d.country_code is null then 'unknown'
        when o.country_code = d.country_code then 'domestic'
        else 'international'
    end as route_scope
from {{ ref('stg_flights') }} as f
left join {{ ref('stg_airports') }} as o on f.origin_icao = o.airport_icao
left join {{ ref('stg_airports') }} as d on f.destination_icao = d.airport_icao
