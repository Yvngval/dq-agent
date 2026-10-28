{#- Test personnalisé : unicité d'une combinaison de colonnes (clé composite). -#}
{% test unique_combination(model, combination) %}
select {{ combination | join(', ') }}, count(*) as n_records
from {{ model }}
group by {{ combination | join(', ') }}
having count(*) > 1
{% endtest %}
