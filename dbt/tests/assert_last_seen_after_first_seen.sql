-- Test personnalisé : un vol ne peut pas finir avant d'avoir commencé.
{{ config(meta={'dq_category': 'date_inconsistency'}) }}
select *
from {{ ref('stg_flights') }}
where last_seen_at < first_seen_at
