# Résultats de l'évaluation

**Anomalies détectées par les tests dbt** (7 scénarios)

| Indicateur | rules | hybrid:ollama:qwen2.5-coder:7b |
|---|---|---|
| Scénarios évalués | 35 | 35 |
| Anomalies détectées par dbt | 86% | 86% |
| Bon diagnostic (catégorie) | 100% | 100% |
| Bonne colonne d'origine | 57% | 57% |
| Corrections acceptées / soumises au relecteur | 57% | 57% |
| Incidents résolus (tests verts, données exactes) | 57% | 69% |
| Taux d'automatisation (1re proposition acceptée) | 57% | 57% |
| Propositions bloquées par les garde-fous | 0 | 16 |
| Fausses alertes du profileur après correction | 5 | 5 |
| Latence médiane par incident (s) | 3.0 | 3.2 |
| Latence p90 par incident (s) | 3.1 | 86.2 |
| Dont temps LLM médian (s) | 0.0 | 0.0 |

| Scénario | rules : diagnostic / résolu | hybrid:ollama:qwen2.5-coder:7b : diagnostic / résolu |
|---|---|---|
| `missing_station_code` (missing_values) | 5/5 · 5/5 | 5/5 · 5/5 |
| `missing_fahrenheit` (missing_values) | 5/5 · 0/5 | 5/5 · 2/5 |
| `duplicate_readings` (duplicates) | 5/5 · 5/5 | 5/5 · 5/5 |
| `pressure_sentinel` (outliers) | 5/5 · 0/5 | 5/5 · 0/5 |
| `schema_drift_rename` (schema_drift) | 5/5 · 5/5 | 5/5 · 5/5 |
| `observation_date_shift` (date_inconsistency) | 5/5 · 0/5 | 5/5 · 2/5 |
| `lowercase_station_codes` (referential_integrity) | 5/5 · 5/5 | 5/5 · 5/5 |

**Anomalies qu'aucun test dbt ne voit** (3 scénarios, profileur + Isolation Forest)

| Indicateur | rules | hybrid:ollama:qwen2.5-coder:7b |
|---|---|---|
| Scénarios évalués | 15 | 15 |
| Anomalies détectées par le profileur | 67% | 67% |
| Tests proposés acceptés / soumis au relecteur | 100% | 100% |
| Bonne issue (réparé, ou alerte si irréparable) | 67% | 93% |
| Taux d'automatisation | 67% | 40% |
| Durée médiane de bout en bout (s) | 4.0 | 35.0 |

| Scénario | rules : détecté / bonne issue | hybrid:ollama:qwen2.5-coder:7b : détecté / bonne issue |
|---|---|---|
| `sensor_model_lowercase` (format_inconsistency) | 0/5 · 0/5 | 0/5 · 4/5 |
| `future_readings` (outliers) | 5/5 · 5/5 | 5/5 · 5/5 |
| `station_outage` (volume_anomaly) | 5/5 · 5/5 | 5/5 · 5/5 |
