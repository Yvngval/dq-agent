# Résultats de l'évaluation

**Anomalies détectées par les tests dbt** (7 scénarios)

| Indicateur | rules | hybrid:ollama:qwen2.5-coder:7b | ollama:qwen2.5-coder:7b |
|---|---|---|---|
| Scénarios évalués | 35 | 35 | 35 |
| Anomalies détectées par dbt | 100% | 100% | 100% |
| Bon diagnostic (catégorie) | 100% | 100% | 100% |
| Bonne colonne d'origine | 71% | 71% | 83% |
| Corrections acceptées / soumises au relecteur | 71% | 75% | 70% |
| Incidents résolus (tests verts, données exactes) | 71% | 86% | 46% |
| Taux d'automatisation (1re proposition acceptée) | 71% | 71% | 37% |
| Propositions bloquées par les garde-fous | 0 | 9 | 43 |
| Fausses alertes du profileur après correction | 0 | 0 | 0 |
| Latence médiane par incident (s) | 3.0 | 3.0 | 55.9 |
| Latence p90 par incident (s) | 3.1 | 45.0 | 81.2 |
| Dont temps LLM médian (s) | 0.0 | 0.0 | 52.9 |

| Scénario | rules : diagnostic / résolu | hybrid:ollama:qwen2.5-coder:7b : diagnostic / résolu | ollama:qwen2.5-coder:7b : diagnostic / résolu |
|---|---|---|---|
| `missing_station_code` (missing_values) | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 3/5 |
| `missing_fahrenheit` (missing_values) | 5/5 · 0/5 | 5/5 · 4/5 | 5/5 · 1/5 |
| `duplicate_readings` (duplicates) | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |
| `pressure_sentinel` (outliers) | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |
| `schema_drift_rename` (schema_drift) | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 1/5 |
| `observation_date_shift` (date_inconsistency) | 5/5 · 0/5 | 5/5 · 1/5 | 5/5 · 1/5 |
| `lowercase_station_codes` (referential_integrity) | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 0/5 |

**Anomalies qu'aucun test dbt ne voit** (3 scénarios, profileur + Isolation Forest)

| Indicateur | rules | hybrid:ollama:qwen2.5-coder:7b | ollama:qwen2.5-coder:7b |
|---|---|---|---|
| Scénarios évalués | 15 | 15 | 15 |
| Anomalies détectées par le profileur | 100% | 100% | 100% |
| Tests proposés acceptés / soumis au relecteur | 100% | 100% | 100% |
| Bonne issue (réparé, ou alerte si irréparable) | 67% | 67% | 40% |
| Taux d'automatisation | 67% | 40% | 7% |
| Durée médiane de bout en bout (s) | 6.8 | 35.6 | 67.5 |

| Scénario | rules : détecté / bonne issue | hybrid:ollama:qwen2.5-coder:7b : détecté / bonne issue | ollama:qwen2.5-coder:7b : détecté / bonne issue |
|---|---|---|---|
| `sensor_model_lowercase` (format_inconsistency) | 5/5 · 0/5 | 5/5 · 0/5 | 5/5 · 1/5 |
| `future_readings` (outliers) | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 0/5 |
| `station_outage` (volume_anomaly) | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |
