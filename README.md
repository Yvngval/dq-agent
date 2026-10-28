<p align="right"><b>English</b> | <a href="./README.fr.md">Français</a></p>

<h1 align="center">Data Quality AI Agent</h1>

<p align="center">
  <a href="https://www.python.org"><img src="https://img.shields.io/badge/Python_3.12-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python"></a>
  <a href="https://www.getdbt.com"><img src="https://img.shields.io/badge/dbt-FF694B?style=for-the-badge&logo=dbt&logoColor=white" alt="dbt"></a>
  <a href="https://duckdb.org"><img src="https://img.shields.io/badge/DuckDB-FFF000?style=for-the-badge&logo=duckdb&logoColor=black" alt="DuckDB"></a>
  <a href="https://www.postgresql.org"><img src="https://img.shields.io/badge/PostgreSQL-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL"></a>
  <a href="https://langchain-ai.github.io/langgraph/"><img src="https://img.shields.io/badge/LangGraph-1C3C3C?style=for-the-badge&logo=langchain&logoColor=white" alt="LangGraph"></a>
  <a href="https://ollama.com"><img src="https://img.shields.io/badge/Ollama-000000?style=for-the-badge&logo=ollama&logoColor=white" alt="Ollama"></a>
  <a href="https://scikit-learn.org"><img src="https://img.shields.io/badge/scikit--learn-F7931E?style=for-the-badge&logo=scikitlearn&logoColor=white" alt="scikit-learn"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="License MIT"></a>
</p>

> **A LangGraph agent that watches a dbt pipeline: it finds the root cause of failing tests, proposes a SQL fix, has a human approve it, applies it and re-runs the tests. When everything is green, it also looks for anomalies no test anticipates and proposes the missing tests.**

Data pipelines rarely break silently: a file arrives with a renamed column, a batch is loaded twice, a sensor reports impossible altitudes. dbt tests flag it, but someone still has to understand the cause and fix it, and tests only catch the errors someone thought of. This project automates that work on real air traffic data (OpenSky Network, flights touching France in April 2020), never changes anything without human approval, and measures what the agent actually gets right on 13 kinds of injected anomalies. The same agent also monitors a second dbt project, weather station readings, without a single extra line of code, on DuckDB as well as PostgreSQL.

<p align="center">
  <a href="#results">Results</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#evaluation">Evaluation</a> ·
  <a href="#data">Data</a> ·
  <a href="#getting-started">Getting started</a> ·
  <a href="#mcp-server">MCP server</a> ·
  <a href="#starting-from-your-own-files-dq-init">dq init</a> ·
  <a href="#databases">Databases</a> ·
  <a href="#using-the-agent-on-another-dbt-project">Another dbt project</a>
</p>

<p align="center">
  <img src="docs/review.svg" alt="Human review screen: diagnosis, proposed SQL fix, dry run on the affected rows" width="820">
</p>

---

## Results

<!-- RESULTS:START -->
### Flights (OpenSky, real data)

Measured on 28/10/2026 at commit `60d82f1`, on an Apple M4 Mac (16 GB): 13 scenarios × 5 seeds, i.e. 65 runs per reasoner, against the simulated reviewer described in [Evaluation](#evaluation). LLM: `qwen2.5-coder:7b`, run locally with Ollama. Full details in [`reports/RESULTS.md`](reports/RESULTS.md) (in French), one line per run in `reports/results_*.jsonl`.

Since then, the agent has been made generic, and only two examples have changed in the text given to the LLM. Rerun on the final version, the rule-based reasoner gets exactly the same outcomes on all 65 runs.

**8 anomalies that dbt tests catch** (40 runs per reasoner)

| Metric | Rules | Hybrid (rules, then LLM) | LLM only |
|---|---|---|---|
| Correct diagnosis (category) | 88% | 88% | 90% |
| Incidents resolved (tests green, data exact) | 62% | **85%** | 55% |
| Automation rate | 62% | 62% | 45% |
| Fixes accepted by the reviewer | 50% | 51% | 59% |
| Proposals blocked by the guardrails | 0 | 14 | 46 |
| Latency per incident, median / p90 | 3.0 s / 3.4 s | 3.1 s / 61 s | 38 s / 176 s |

**5 anomalies no dbt test catches** (25 runs per reasoner)

| Metric | Rules | Hybrid (rules, then LLM) | LLM only |
|---|---|---|---|
| Anomaly spotted by the profiler | 92% | 92% | 92% |
| Proposed tests accepted by the reviewer | 83% | 45% | 48% |
| Correct outcome (repaired, or alerted if irreparable) | 72% | **76%** | 56% |
| Automation rate | 64% | 20% | 0% |
| Median end-to-end time | 4.3 s | 35 s | 67 s |

The profiler raised no false alert on the scenarios dbt catches (0 out of 120 runs).

<details>
<summary>Per-scenario details</summary>

| Scenario | Rules: diagnosis · resolved | Hybrid: diagnosis · resolved | LLM only: diagnosis · resolved |
|---|---|---|---|
| `missing_flight_date` | 5/5 · 0/5 | 5/5 · 4/5 | 5/5 · 0/5 |
| `missing_aircraft_id` | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 4/5 |
| `duplicate_rows` | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |
| `altitude_outlier` | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |
| `schema_drift_rename` | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |
| `swapped_timestamps` | 0/5 · 0/5 | 0/5 · 2/5 | 1/5 · 1/5 |
| `flight_date_shift` | 5/5 · 0/5 | 5/5 · 3/5 | 5/5 · 2/5 |
| `lowercase_airport_codes` | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 0/5 |

| Scenario | Rules: spotted · correct outcome | Hybrid: spotted · correct outcome | LLM only: spotted · correct outcome |
|---|---|---|---|
| `typecode_lowercase` | 5/5 · 0/5 | 5/5 · 1/5 | 5/5 · 0/5 |
| `future_timestamps` | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 1/5 |
| `swapped_coordinates` | 3/5 · 3/5 | 3/5 · 3/5 | 3/5 · 3/5 |
| `typecode_null_spike` | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |
| `volume_drop` | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |

</details>

**Key takeaways**

- **The hybrid reasoner is the best trade-off.** Rules handle known cases in 3 seconds; the LLM only takes over when a rule is rejected or missing. It thus resolves incidents the rules give up on (`missing_flight_date` 4/5 vs 0/5, `flight_date_shift` 3/5 vs 0/5, `swapped_timestamps` 2/5 vs 0/5), with the same median latency.
- **On its own, the local LLM does worse than the rules**, despite an equally good diagnosis: fixing is where it struggles (46 proposals blocked by the guardrails, `lowercase_airport_codes` 0/5). At 7 billion parameters, it is best used as a fallback.
- **The cost of the hybrid on anomalies without a test.** When the rules judge an anomaly irreparable (NULL spike, volume drop), the hybrid still hands over to the LLM, which proposes up to 3 rejected tests before raising the same alert. The outcome is still correct, but automation drops from 64% to 20%. Trusting the rules' escalation is the next improvement.
- **Measured weak spots.** `typecode_lowercase`: the new test is accepted 15 times out of 15, but the upper-casing that follows only succeeds once in 15. `swapped_coordinates`: the Isolation Forest spots the swap only 3 times out of 5. `swapped_timestamps`: the failing duration test points towards an outlier, hence 1 correct diagnosis out of 15.
- **Progress since the first version**, on the same 8 scenarios: the LLM alone goes from 42% to 55% of incidents resolved and from 59 s to 38 s median latency. In v1, letting it write free SQL instead of choosing a typed action dropped automation from 35% to 6% and acceptance from 49% to 37%. See [`reports/v1/RESULTS.md`](reports/v1/RESULTS.md) (in French).

### Second dbt project: weather stations

Measured on 28/10/2026 at commit `b2a1d34`, on the same Mac and with the same LLM: 10 scenarios × 5 seeds, i.e. 50 runs per reasoner, on synthetic data (32 stations, 10,752 hourly readings). Details in [`reports/weather/RESULTS.md`](reports/weather/RESULTS.md) (in French).

**7 anomalies that dbt tests catch** (35 runs per reasoner)

| Metric | Rules | Hybrid (rules, then LLM) | LLM only |
|---|---|---|---|
| Correct diagnosis (category) | 100% | 100% | 100% |
| Incidents resolved (tests green, data exact) | 71% | **86%** | 46% |
| Automation rate | 71% | 71% | 37% |
| Fixes accepted by the reviewer | 71% | 75% | 70% |
| Proposals blocked by the guardrails | 0 | 9 | 43 |
| Latency per incident, median / p90 | 3.0 s / 3.1 s | 3.0 s / 45 s | 56 s / 81 s |

**3 anomalies no dbt test catches** (15 runs per reasoner)

| Metric | Rules | Hybrid (rules, then LLM) | LLM only |
|---|---|---|---|
| Anomaly spotted by the profiler | 100% | 100% | 100% |
| Proposed tests accepted by the reviewer | 100% | 100% | 100% |
| Correct outcome (repaired, or alerted if irreparable) | 67% | 67% | 40% |
| Automation rate | 67% | 40% | 7% |
| Median end-to-end time | 6.8 s | 36 s | 68 s |

<details>
<summary>Per-scenario details</summary>

| Scenario | Rules: diagnosis · resolved | Hybrid: diagnosis · resolved | LLM only: diagnosis · resolved |
|---|---|---|---|
| `missing_station_code` | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 3/5 |
| `missing_fahrenheit` | 5/5 · 0/5 | 5/5 · 4/5 | 5/5 · 1/5 |
| `duplicate_readings` | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |
| `pressure_sentinel` | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |
| `schema_drift_rename` | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 1/5 |
| `observation_date_shift` | 5/5 · 0/5 | 5/5 · 1/5 | 5/5 · 1/5 |
| `lowercase_station_codes` | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 0/5 |

| Scenario | Rules: spotted · correct outcome | Hybrid: spotted · correct outcome | LLM only: spotted · correct outcome |
|---|---|---|---|
| `sensor_model_lowercase` | 5/5 · 0/5 | 5/5 · 0/5 | 5/5 · 1/5 |
| `future_readings` | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 0/5 |
| `station_outage` | 5/5 · 5/5 | 5/5 · 5/5 | 5/5 · 5/5 |

</details>

**Key takeaways**

- **The same trends as on flights, without touching the agent's code.** The hybrid reasoner resolves the most incidents: 86%, against 71% for the rules and 46% for the LLM alone. The LLM takes over when the generic rule fails, for instance to recompute `temperature_f` from `temperature_c` thanks to the column description (4/5 vs 0/5).
- **The same weak spots too.** `observation_date_shift` is repaired only once in 5 by the hybrid. For `sensor_model_lowercase`, the new test is accepted 15 times out of 15, but upper-casing only succeeds once in 15. On the station outage, the hybrid hands over to the LLM, which proposes up to 3 rejected tests before raising the same alert as the rules (automation 40% vs 67%).
- **PostgreSQL.** The same project rerun on PostgreSQL gives exactly the same outcomes as on DuckDB for all 50 runs of the rule-based reasoner (median latency per incident: 2.9 s, against 3.0 s on DuckDB).
- **Take the LLM-only timings with caution**: part of its evaluation ran while other jobs were using the machine.
<!-- RESULTS:END -->

---

## How it works

The agent is a LangGraph graph with two loops. The **repair loop** starts from a failing dbt test. The **monitoring loop** starts when every test passes: a statistical profile looks for what the tests miss, and each finding can become a new test once a human agrees.

```mermaid
flowchart LR
    A[run_checks<br/>dbt build] -->|failures| B[select_incident]
    B --> C[gather_context<br/>read-only tools]
    C --> D[diagnose]
    D --> E[propose_fix<br/>typed action]
    E --> F{validate_fix<br/>guardrails + dry run}
    F -->|rejected, with reason| E
    F -->|valid| G{{human_review<br/>interrupt}}
    G -->|rejected + comment| E
    G -->|approved or edited| H[apply_fix<br/>quarantine, then execute]
    H --> A
    G -->|escalated| X[escalate]
    X --> B
    A -->|all green| P[profile<br/>statistics + Isolation Forest]
    P -->|nothing to report| Z([end])
    P -->|finding| T[propose_test]
    T --> V{validate_test<br/>today / history}
    V --> R{{review_test<br/>interrupt}}
    R -->|approved| W[write_test<br/>dbt/tests/generated]
    W --> A
    R -->|human alert| Q[dismiss_finding]
    T -->|no row rule possible| Q
    Q --> P
```

**Repair loop**

1. **dbt pipeline on DuckDB.** `raw` landing zone (flights and airports) → typed `staging` views → `marts` tables (enriched flights, daily traffic per French airport). 4 models and 28 tests: `not_null`, `unique`, `accepted_values`, `relationships`, and 4 custom tests (`in_range` for physical measurements, `unique_combination`, two date-consistency tests).
2. **Detection.** `run_checks` runs `dbt build`, then reads `run_results.json` and `manifest.json`: failing test, column, row count, and the column's lineage back to the `raw` table (declared in dbt `meta`, or inferred from the dbt graph).
3. **Context.** Read-only tools gather the failing rows (dbt `store_failures` tables), the matching `raw` rows, the source schema contract, the actual schema and column profiles. The LLM gets a compact view (about 1,200 tokens).
4. **Diagnosis.** The incident is classified (missing values, duplicates, outliers, schema drift, date inconsistency, referential integrity, format) and the `raw` columns at fault are named, as structured JSON.
5. **Fix.** The LLM picks a **typed action** (recompute, swap two columns, set to NULL, normalise a code, deduplicate, rename a column, delete rows, free SQL, escalate); the SQL is generated from that choice. Column names are constrained by a JSON schema built from the actual schema, and staging names used by mistake are translated into `raw` expressions using the dbt models' SQL.
6. **Guardrails.** A single `UPDATE`, `DELETE` or `ALTER TABLE ... RENAME COLUMN` statement, on a source table declared in dbt, with a `WHERE` clause, never touching `_row_id`, on at most 10% of the rows. The fix is dry-run in a rolled-back transaction; an error, a proposal already rejected or incomplete coverage of the failing rows goes back to the reasoner, which tries again (3 attempts at most).
7. **Human review.** The graph stops (`interrupt`) and shows the diagnosis, the SQL and a before/after preview. The human approves, rejects with a comment (passed to the agent), edits the SQL or escalates. With a SQLite checkpointer, an interrupted run can be resumed later (`dq run --resume <id>`).
8. **Apply and verify.** Affected rows are copied as JSON to `audit.quarantine`, the fix runs in a transaction, then `dbt build` runs again. Every step is written to `logs/decisions.jsonl` (readable with `dq audit` or DuckDB).

**Monitoring loop**

9. **Reference profile.** For each validated version of the data, the profiler records each column's NULL rate, value range, letter case, length and known categories, and trains an **Isolation Forest per pair of numeric columns** (threshold set on the 1% most isolated reference rows).
10. **Findings.** When every test passes, it compares the current data with that profile: volume, NULL increase, out-of-range values, new case or length, new categories, and an excess of unusual combinations (more than 4 standard deviations above chance). On clean data it reports nothing.
11. **New test.** The agent turns a finding into a dbt test (a SQL condition selecting the faulty rows), measures it on today's data and on the validated history, and shows it to the human, who adds, rejects, edits it or turns it into an alert. An accepted test is written to `dbt/tests/generated/`, then the repair loop takes over. Findings that cannot become a row-level rule (volume, NULL rate, unusual combinations) become **alerts** for a human.

<p align="center">
  <img src="docs/new_test.svg" alt="New dbt test proposed after a profiler finding: SQL condition, rows flagged today and in the history" width="820">
</p>

**Three interchangeable reasoners.** `rules` applies one generic remediation per test type (no-LLM baseline, used by CI); `ollama` uses a local LLM (default `qwen2.5-coder:7b`, no data leaves the machine); `hybrid` tries the rule first, then hands over to the LLM if the rule is rejected or missing. Claude is available through the API (`anthropic`, `hybrid:anthropic`, model `claude-opus-5-5`).

---

## Evaluation

Each scenario starts from a clean baseline, injects an anomaly with a fixed seed and writes the expected state after a correct fix. The agent then runs end to end against a **simulated reviewer**: it applies the proposed fix in a rolled-back transaction and accepts it only if the resulting data is exactly the expected data. A proposed test is accepted only if it flags every injected row and no row of the validated history. Otherwise the reviewer rejects, with the kind of comment a human would make ("45 rows deleted although they could have been repaired"), without giving away the answer.

**8 anomalies that dbt tests catch**

| Scenario | Category | Injected anomaly | Correct fix |
|---|---|---|---|
| `missing_flight_date` | missing values | `day` set to NULL | recompute from `lastseen` |
| `missing_aircraft_id` | missing values | `icao24` set to NULL | quarantine the rows |
| `duplicate_rows` | duplicates | batch loaded twice | delete the copies |
| `altitude_outlier` | outliers | 99,999 m or unit error | set the measurement to NULL |
| `schema_drift_rename` | schema drift | renamed column | rename it back |
| `swapped_timestamps` | date inconsistency | `firstseen` and `lastseen` swapped | swap them back |
| `flight_date_shift` | date inconsistency | `day` shifted by one day | recompute from `lastseen` |
| `lowercase_airport_codes` | referential integrity | lower-case ICAO codes | upper-case them |

**5 anomalies no dbt test catches**

| Scenario | Injected anomaly | Correct outcome |
|---|---|---|
| `typecode_lowercase` | aircraft types in lower case | new test, then upper-case them |
| `future_timestamps` | flights dated 10 or 20 years ahead | new test, then quarantine |
| `swapped_coordinates` | departure latitude and longitude swapped | alert (Isolation Forest) |
| `typecode_null_spike` | aircraft type missing on 40 to 60% of flights instead of 27% | alert |
| `volume_drop` | 30 to 50% of flights missing | alert |

**Second dataset: 10 anomalies on weather stations** ([`examples/weather`](examples/weather), synthetic data)

| Scenario | Injected anomaly | Correct outcome |
|---|---|---|
| `missing_station_code` | reading's station set to NULL | quarantine the rows |
| `missing_fahrenheit` | `temperature_f` set to NULL | recompute from `temperature_c` |
| `duplicate_readings` | batch sent twice | delete the copies |
| `pressure_sentinel` | faulty barometer: 0 or 9,999.9 hPa | set the measurement to NULL |
| `schema_drift_rename` | column renamed by the provider | rename it back |
| `observation_date_shift` | `obs_date` in local time, one day off | recompute from `observed_at` |
| `lowercase_station_codes` | lower-case station codes | upper-case them |
| `sensor_model_lowercase` *(no test)* | sensor models in lower case | new test, then upper-case them |
| `future_readings` *(no test)* | clock drift: readings 10 or 20 years ahead | new test, then quarantine |
| `station_outage` *(no test)* | 30 to 50% of readings missing | alert |

Metrics: correct **diagnosis** (category); **accepted fixes** over fixes submitted to the reviewer; **resolved incident** (tests green and data exactly restored); **correct outcome** for anomalies without a test (repaired, or alerted when it cannot be repaired); **automation rate** (correct outcome reached with the first proposal, no rejection, edit or escalation); **latency** from picking up the incident to the tests passing again, excluding reviewer time, on an Apple M4 Mac (16 GB).

**Limitations.** The reviewer is simulated: it knows the ground truth, which a human does not. Anomalies are injected one at a time and stay close to textbook cases. The rule-based reasoner was written by the author of the scenarios: it is a strong baseline on the cases it covers, not a neutral opponent. The source column descriptions (for example "`day` is always `CAST(lastseen AS DATE)`") are read by the LLM and genuinely contribute to its correct diagnoses. The weather dataset is synthetic and its scenarios were written by the author of the agent, avoiding the names used in the examples given to the LLM. The local LLM is a 7-billion-parameter model; a larger model or Claude would probably do better, but that is not measured here.

---

## Data

- **Flights**: *Crowdsourced air traffic data from The OpenSky Network 2020*, April 2020 file (Zenodo, [10.5281/zenodo.7923702](https://doi.org/10.5281/zenodo.7923702)), filtered to the 7,626 flights departing from or arriving at a French airport.
- **Airports**: [OurAirports](https://ourairports.com/data/) (public domain), 86,293 airports.

The OpenSky licence allows non-profit research and education use but **forbids redistribution**: the data is not in this repository, `dq download` fetches it from the source. Aircraft identifiers (`icao24`) and private flight callsigns are **pseudonymised** at ingestion with a hash salted by a local key, and registrations are dropped. CI runs on synthetic flights.

**What the tests found in the real data.** Before any injection, 5 tests fail on 57 flights (0.7%): 34 airport codes missing from the reference table (old codes such as EDDT for Berlin Tegel), 17 flights longer than 20 hours (tracks of several flights stitched together) and 6 altitudes above 16 km. These rows are set aside, with their reason, in `audit.baseline_exclusions`: this is the clean baseline for the evaluation (`dq ingest --no-certify` keeps them, to try the agent on real anomalies).

> Matthias Schäfer, Martin Strohmeier, Vincent Lenders, Ivan Martinovic, Matthias Wilhelm. *Bringing up OpenSky: A large-scale ADS-B sensor network for research.* IPSN 2014. Data: The OpenSky Network, https://opensky-network.org

---

## Getting started

Requirements: [uv](https://docs.astral.sh/uv/) and, for the local LLM, [Ollama](https://ollama.com).

```bash
git clone https://github.com/Yvngval/dq-agent.git && cd dq-agent
uv sync
ollama pull qwen2.5-coder:7b        # local LLM (4.7 GB)
make data ingest check              # download, load, dbt build: everything is green
make demo                           # inject an anomaly and run the agent; you approve in the terminal
```

Without downloads or an LLM: `make synthetic`, then `PYTHONPATH=src uv run dq run --provider rules`. For the weather dataset: `make weather weather-demo`.

| Command | Purpose |
|---|---|
| `dq init <files> [--yes]` | Create a monitored project from files; the agent proposes rules, you approve |
| `dq baseline` | Record the current state of the sources as the clean reference (copy and statistical profile) |
| `dq inject --list` | List the scenarios of the evaluated dataset; `dq inject <scenario> --seed N` injects one |
| `dq reset` | Restore the sources from the reference |
| `dq run [--provider rules\|ollama\|hybrid\|anthropic]` | Run the agent with review in the terminal |
| `dq profile` | Compare the data with the reference profile, without changing anything |
| `dq evaluate --provider ollama --seeds 5 [--suite v1\|v2]` | Evaluate the agent against the simulated reviewer |
| `dq report [files] [--out]` | Write `reports/RESULTS.md` |
| `dq audit` | Show the decision log |
| `dq graph` | Print the LangGraph graph as Mermaid |
| `make test` | pytest suite (synthetic data) |

For Claude: copy `.env.example` to `.env`, set `DQ_LLM_PROVIDER=anthropic` (or `hybrid:anthropic`) and provide the key in `ANTHROPIC_API_KEY`.

---

## MCP server

`dq mcp` starts a read-only [MCP](https://modelcontextprotocol.io) server, usable from Claude Desktop or Claude Code: `list_tables`, `describe_table`, `source_contract` (gap between the contract and the actual schema), `test_results` (last `dbt build`), `failing_rows`, `column_profile`, `profile_findings` (anomalies without a test), `decisions` (agent log) and `quarantine`.

```json
{"mcpServers": {"dq-agent": {"command": "uv", "args": ["run", "--directory", "/path/to/dq-agent", "dq", "mcp"],
                             "env": {"PYTHONPATH": "/path/to/dq-agent/src"}}}}
```

---

## Starting from your own files: `dq init`

No dbt project yet? `dq init` starts from CSV, Parquet or JSON files. The agent loads each file into a DuckDB table, looks for **candidate rules** in the data and proposes them one by one: you decide. Accepted rules become dbt tests, and their text becomes the column documentation that the LLM later reads to diagnose and repair.

<p align="center">
  <img src="docs/init.svg" alt="dq init: the agent proposes the temperature_f formula (true for 99.97% of rows, 3 rows violate it), the link between obs_date and observed_at and the station_code key; the human accepts" width="820">
</p>

| Rule searched for | Found on the weather readings |
|---|---|
| never NULL, unique identifier | `reading_id` |
| list of accepted values (few distinct values) | `sensor_model`: 4 models |
| never negative, always upper case | `wind_speed_kmh`, `station_code` |
| date equal to the day of a timestamp | `obs_date` = `CAST(observed_at AS DATE)` |
| column computed from another | `temperature_f` = `round(temperature_c * 1.8 + 32, 1)` |
| value present in another table's key | `station_code` exists in `stations` |
| a date never before another | none here |

A rule that holds for at least 99% of rows is proposed too, with the number of rows that violate it: if you accept it, those rows are probably errors, and `dq run` will have them repaired. The decision stays human for a good reason: on these readings, the agent also proposes "latitude is never negative" or "the Fahrenheit temperature is never negative", true in this March data from Europe but not business rules.

```bash
uv run python examples/weather/generate.py --csv projects/csv    # two CSV files
uv run dq init projects/csv/stations.csv projects/csv/readings.csv --dir projects/weather-init
source projects/weather-init/dq.env && uv run dq run --provider hybrid
```

`make init-demo` runs the first two steps; `--yes` accepts the rules that hold on every row without asking. The generated project is an ordinary dbt project (documented sources, staging models with their lineage, tests), meant to be reviewed and extended.

**Measurement.** The project generated from the two CSV files with `--yes` (the 34 exact rules accepted without asking, no edits) was run against the same 10 failures as the hand-written weather project, 50 runs per reasoner. Measured on 28/10/2026, details in [`reports/weather/init/RESULTS.md`](reports/weather/init/RESULTS.md) (in French).

| Correct outcome on the 10 failures (repaired, or alerted if irreparable) | Rules | Hybrid |
|---|---|---|
| Hand-written project (`examples/weather`) | 70% | 80% |
| Project generated by `dq init`, no edits | 60% | 76% |

- **What it lacks**: `dq init` does not propose plausible bounds, only "never negative". A pressure of 9,999.9 hPa therefore passes all its tests (`pressure_sentinel`: 0 out of 5), whereas the hand-written project bounds pressure between 870 and 1,085 hPa. That is the kind of rule a human adds when reviewing the project.
- **What it brings**: the "always upper case" rule on `sensor_model` makes dbt catch the failure, and its description guides the LLM. The hybrid reasoner thus repairs `sensor_model_lowercase` 4 times out of 5, against 0 out of 5 in the hand-written project, which had no test on that column.

---

## Databases

| Database | Status | Connection |
|---|---|---|
| DuckDB | supported, tested | default: a local file (`DQ_DUCKDB_PATH`) |
| PostgreSQL | supported, tested (pytest locally and in CI, evaluation above) | `DQ_WAREHOUSE=postgresql://user:password@host:5432/database` |
| MySQL | connection written, **not tested** | `DQ_WAREHOUSE=mysql://user:password@host:3306/database` |
| SQLite, Snowflake, BigQuery, Redshift… | not supported | see below |

**How it works.** dbt runs with the database's adapter (dbt-duckdb, dbt-postgres…): detection is always done by the dbt tests. The agent reads and fixes the data through DuckDB, which attaches the database with its extension (`postgres`, `mysql`). Its SQL, guardrails, dry run in a rolled-back transaction and quarantine are therefore the same everywhere. Three differences are handled: PostgreSQL refuses to replace a table that views depend on (the agent drops it with CASCADE, and the next `dbt build` recreates the views); tests generated by the agent are translated into the database's SQL dialect with sqlglot; the profiler works on a local copy read only once.

```bash
uv sync --extra postgres          # dbt-postgres adapter
createdb dq_weather               # empty PostgreSQL database
make weather-postgres             # weather readings in PostgreSQL, reference, dbt build
```

**Not supported yet.** SQLite has no schemas (`raw.readings` does not exist there) and would need tables to be named differently. Snowflake, BigQuery or Redshift would need another connection layer (translating the agent's SQL, or a dedicated DuckDB extension): this is neither done nor tested.

---

## Using the agent on another dbt project

The agent does not know the domain of the data: it reads the dbt project it is pointed at. The tables it may fix are the **sources declared in dbt**, the processing order comes from the **dbt graph**, and the description of the domain given to the LLM is a dbt variable. The [`examples/weather`](examples/weather) folder proves it: a second dbt project, hourly readings from 32 weather stations, that the same agent monitors and repairs without a single extra line of code. Only environment variables change:

```bash
export PYTHONPATH=src DQ_DBT_DIR=examples/weather/dbt DQ_DATA_DIR=examples/weather/data
uv run python examples/weather/generate.py     # load the readings into DuckDB
uv run dq baseline                             # clean reference and statistical profile
uv run dq check                                # dbt build: everything is green
export DQ_SCENARIOS=examples/weather/scenarios.py
uv run dq inject missing_fahrenheit --seed 1   # break the data
uv run dq run --provider hybrid                # the agent repairs, you approve
```

For another dbt project you need:

1. `DQ_DBT_DIR` pointing to the project, then the database: for DuckDB, `DQ_DUCKDB_PATH` (read in `profiles.yml` with `env_var('DQ_DUCKDB_PATH')`); for PostgreSQL, `DQ_WAREHOUSE` and the matching dbt target in `DQ_DBT_PROFILE_TARGET` (the profile can read the connection settings from `DQ_WAREHOUSE_HOST`, `_PORT`, `_USER`, `_PASSWORD` and `_DBNAME`, which the agent derives from `DQ_WAREHOUSE`); `DQ_DBT_PROFILES_DIR` if `profiles.yml` lives elsewhere;
2. **dbt tests**, that is the detectors: the agent fixes what the tests see, and the profiler spots what they miss;
3. **described source columns** in the dbt YAML: this is the schema contract, where the LLM reads for instance that "`obs_date` is always `CAST(observed_at AS DATE)`";
4. **column lineage** for staging models (`meta: {source_table, source_columns}`), otherwise the agent falls back to the single upstream source;
5. **a `_row_id` technical column** in each source table (quarantine, deduplication, preview);
6. for each singular test, its category: `{{ config(meta={'dq_category': 'date_inconsistency'}) }}`; generic tests (`not_null`, `unique`, `relationships`, `accepted_values`, `in_range`) are recognised out of the box;
7. the `dq_agent.domain` variable in `dbt_project.yml`, then `dq baseline` once the data is deemed clean.

Failing rows are always kept (`dbt build --store-failures`). To measure the agent on a new project, a scenario file (`DQ_SCENARIOS`, modelled on [`examples/weather/scenarios.py`](examples/weather/scenarios.py)) describes the anomalies to inject and the expected state after a correct fix.

---

## Project structure

```text
dbt/                    flights dbt project: sources (contract), staging, marts, custom and generated tests
examples/weather/       second dbt project (weather stations): generator, dbt project, anomaly scenarios
src/dq_agent/
  ingest.py             OpenSky + OurAirports loading, pseudonymisation, certification
  reference.py          clean reference of the dbt sources (copy and profile), restore
  discovery.py          candidate rule discovery in the data (`dq init`)
  bootstrap.py          `dq init`: file loading, rule review, generated dbt project
  anomalies.py          injection tools, flight scenarios and their ground truth
  dbt_runner.py         dbt build, run_results.json, lineage and dbt graph parsing
  context.py            read-only context tools
  reasoners.py          reasoners: rules, LLM (typed actions or free SQL), hybrid
  actions.py            typed fix actions and SQL generation
  guardrails.py         SQL checks, dry run, quarantine, apply
  profiler.py           reference profile, statistical findings, Isolation Forest
  monitoring.py         from findings to dbt tests: validation, writing
  graph.py              LangGraph graph and human review loops
  review.py             reviewers: terminal, simulated (evaluation)
  evaluate.py           measured evaluation
  mcp_server.py         read-only MCP server
  warehouse.py          connection: DuckDB file, or PostgreSQL and MySQL attached through DuckDB
tests/                  pytest suite (synthetic data, flights and weather)
reports/                evaluation results (JSONL) and RESULTS.md; reports/weather/: weather dataset; reports/v1/: first version
```

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

The code is released under the [MIT License](LICENSE). OpenSky data remains subject to the OpenSky Network licence; OurAirports is in the public domain.

## Authors

<a href="https://github.com/Yvngval"><img src="https://github.com/Yvngval.png?size=96" width="64" height="64" alt="Yvngval"></a>

Valentin Ponnoussamy ([@Yvngval](https://github.com/Yvngval)), Master's in Data, Knowledge and Intelligence, Université Paris Cité, 2026.
