# Résultats de l'évaluation

**Anomalies détectées par les tests dbt** (7 scénarios)

| Indicateur | rules |
|---|---|
| Scénarios évalués | 35 |
| Anomalies détectées par dbt | 100% |
| Bon diagnostic (catégorie) | 100% |
| Bonne colonne d'origine | 71% |
| Corrections acceptées / soumises au relecteur | 71% |
| Incidents résolus (tests verts, données exactes) | 71% |
| Taux d'automatisation (1re proposition acceptée) | 71% |
| Propositions bloquées par les garde-fous | 0 |
| Fausses alertes du profileur après correction | 0 |
| Latence médiane par incident (s) | 2.9 |
| Latence p90 par incident (s) | 3.0 |
| Dont temps LLM médian (s) | 0.0 |

| Scénario | rules : diagnostic / résolu |
|---|---|
| `missing_station_code` (missing_values) | 5/5 · 5/5 |
| `missing_fahrenheit` (missing_values) | 5/5 · 0/5 |
| `duplicate_readings` (duplicates) | 5/5 · 5/5 |
| `pressure_sentinel` (outliers) | 5/5 · 5/5 |
| `schema_drift_rename` (schema_drift) | 5/5 · 5/5 |
| `observation_date_shift` (date_inconsistency) | 5/5 · 0/5 |
| `lowercase_station_codes` (referential_integrity) | 5/5 · 5/5 |

**Anomalies qu'aucun test dbt ne voit** (3 scénarios, profileur + Isolation Forest)

| Indicateur | rules |
|---|---|
| Scénarios évalués | 15 |
| Anomalies détectées par le profileur | 100% |
| Tests proposés acceptés / soumis au relecteur | 100% |
| Bonne issue (réparé, ou alerte si irréparable) | 67% |
| Taux d'automatisation | 67% |
| Durée médiane de bout en bout (s) | 6.8 |

| Scénario | rules : détecté / bonne issue |
|---|---|
| `sensor_model_lowercase` (format_inconsistency) | 5/5 · 0/5 |
| `future_readings` (outliers) | 5/5 · 5/5 |
| `station_outage` (volume_anomaly) | 5/5 · 5/5 |
