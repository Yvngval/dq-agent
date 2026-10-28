-- Test personnalisé : flight_date est, par définition, le jour UTC du dernier message reçu.
select *
from {{ ref('stg_flights') }}
where flight_date <> cast(last_seen_at as date)
