-- Test personnalisé : observation_date est, par définition, le jour UTC de l'horodatage.
{{ config(meta={'dq_category': 'date_inconsistency'}) }}
select *
from {{ ref('stg_readings') }}
where observation_date <> cast(observed_at_utc as date)
