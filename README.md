<p align="right"><b>English</b> | <a href="./README.fr.md">Français</a></p>

<h1 align="center">Data Quality AI Agent</h1>

<p align="center">
  <a href="https://www.python.org"><img src="https://img.shields.io/badge/Python_3.12-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python"></a>
  <a href="https://www.getdbt.com"><img src="https://img.shields.io/badge/dbt-FF694B?style=for-the-badge&logo=dbt&logoColor=white" alt="dbt"></a>
  <a href="https://duckdb.org"><img src="https://img.shields.io/badge/DuckDB-FFF000?style=for-the-badge&logo=duckdb&logoColor=black" alt="DuckDB"></a>
  <a href="https://langchain-ai.github.io/langgraph/"><img src="https://img.shields.io/badge/LangGraph-1C3C3C?style=for-the-badge&logo=langchain&logoColor=white" alt="LangGraph"></a>
  <a href="https://ollama.com"><img src="https://img.shields.io/badge/Ollama-000000?style=for-the-badge&logo=ollama&logoColor=white" alt="Ollama"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="License MIT"></a>
</p>

> **A LangGraph agent that reads failing dbt tests, finds the root cause in the source data, proposes a SQL fix, waits for a human to approve it, applies it and re-runs the tests.**

Data pipelines rarely break silently: a file arrives with a renamed column, a batch is loaded twice, a sensor reports impossible altitudes. dbt tests flag it, but someone still has to understand the cause and fix it. This project automates that work on real air traffic data (OpenSky Network, flights touching France in April 2020), never changes anything without human approval, and measures what the agent actually gets right on 8 kinds of injected anomalies.

<p align="center">
  <a href="#results">Results</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#evaluation">Evaluation</a> ·
  <a href="#data">Data</a> ·
  <a href="#getting-started">Getting started</a> ·
  <a href="#mcp-server">MCP server</a>
</p>

<p align="center">
  <img src="docs/review.svg" alt="Human review screen: diagnosis, proposed SQL fix, dry run on the affected rows" width="820">
</p>

---

## Results

<!-- RESULTS:START -->
<!-- RESULTS:END -->

---

## How it works

```mermaid
flowchart LR
    A[run_checks<br/>dbt build] -->|all green| Z([end])
    A -->|failures| B[select_incident]
    B --> C[gather_context<br/>read-only tools]
    C --> D[diagnose<br/>LLM]
    D --> E[propose_fix<br/>LLM: typed action]
    E --> F{validate_fix<br/>guardrails + dry run}
    F -->|rejected, with the reason| E
    F -->|valid| G{{human_review<br/>LangGraph interrupt}}
    G -->|rejected + comment| E
    G -->|approved or edited| H[apply_fix<br/>quarantine, then execute]
    H --> A
    G -->|escalated| X[escalate]
    F -->|3 failures| X
    X --> B
```

1. **dbt pipeline on DuckDB.** `raw` landing zone (flights and airports) → typed `staging` views → `marts` tables (enriched flights, daily traffic per French airport). 4 models and 28 tests: `not_null`, `unique`, `accepted_values`, `relationships`, and 4 custom tests (`in_range` for physical measurements, `unique_combination`, two date-consistency tests).
2. **Detection.** `run_checks` runs `dbt build`, then reads `run_results.json` and `manifest.json`: failing test, column, row count, and the column's lineage back to the `raw` table (declared in dbt `meta`).
3. **Context.** Read-only tools gather the failing rows (dbt `store_failures` tables), the matching `raw` rows, the source schema contract, the actual schema and column profiles. The LLM gets a compact view (about 1,200 tokens).
4. **Diagnosis.** The LLM classifies the incident (missing values, duplicates, outliers, schema drift, date inconsistency, referential integrity) and names the `raw` columns at fault, as structured JSON.
5. **Fix.** The LLM picks a **typed action** (recompute, swap two columns, set to NULL, normalise a code, deduplicate, rename a column, delete rows, free SQL, escalate); the SQL is generated from that choice. Column names are constrained by a JSON schema built from the actual schema, and staging names used by mistake are translated into `raw` expressions using the dbt models' SQL.
6. **Guardrails.** A single `UPDATE`, `DELETE` or `ALTER TABLE ... RENAME COLUMN` statement, on a `raw` table, with a `WHERE` clause, never touching `_row_id`, on at most 10% of the rows. The fix is dry-run in a rolled-back transaction; an error or incomplete coverage of the failing rows goes back to the LLM, which tries again (3 attempts at most).
7. **Human review.** The graph stops (`interrupt`) and shows the diagnosis, the SQL and a before/after preview. The human approves, rejects with a comment (passed to the agent), edits the SQL or escalates. With a SQLite checkpointer, an interrupted run can be resumed later (`dq run --resume <id>`).
8. **Apply and verify.** Affected rows are copied as JSON to `audit.quarantine`, the fix runs in a transaction, then `dbt build` runs again. Every step is written to `logs/decisions.jsonl` (readable with `dq audit` or DuckDB).

The LLM is configurable: **Ollama** locally (default `qwen2.5-coder:7b`, no data leaves the machine) or **Claude** through the API (`DQ_LLM_PROVIDER=anthropic`, model `claude-opus-5-5`). A **rule-based** reasoner, with no LLM, serves as a baseline and runs in CI.

---

## Evaluation

Each scenario starts from a clean baseline, injects an anomaly with a fixed seed and writes the expected state after a correct fix. The agent then runs end to end against a **simulated reviewer**: it applies the proposed fix in a rolled-back transaction and accepts it only if the resulting data is exactly the expected data. Otherwise it rejects it with the kind of comment a human would make ("45 rows deleted although they could have been repaired"), without giving away the answer.

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

Metrics: correct **diagnosis** (category); **accepted fixes** over fixes submitted to the reviewer; **resolved incident** (tests green and data exactly restored); **automation rate** (resolved with the first proposal, no rejection, edit or escalation); agent **latency**, from picking up the incident to the tests passing again, excluding reviewer time, on an Apple M4 Mac (16 GB).

**Limitations.** The reviewer is simulated: it knows the ground truth, which a human does not. Anomalies are injected one at a time and stay close to textbook cases. The rule-based reasoner was written by the author of the scenarios: it is a strong baseline on the cases it covers, not a neutral opponent. The source column descriptions (for example "`day` is always `CAST(lastseen AS DATE)`") are read by the LLM and genuinely contribute to its correct diagnoses.

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

Without downloads or an LLM: `make synthetic`, then `PYTHONPATH=src uv run dq run --provider rules`.

| Command | Purpose |
|---|---|
| `dq inject --list` | List the 8 scenarios; `dq inject <scenario> --seed N` injects one |
| `dq run [--provider ollama\|anthropic\|rules]` | Run the agent with review in the terminal |
| `dq evaluate --provider ollama --seeds 5` | Evaluate the agent against the simulated reviewer |
| `dq report` | Write `reports/RESULTS.md` |
| `dq audit` | Show the decision log |
| `dq graph` | Print the LangGraph graph as Mermaid |
| `make test` | pytest suite (synthetic data) |

For Claude: copy `.env.example` to `.env`, set `DQ_LLM_PROVIDER=anthropic` and provide the key in `ANTHROPIC_API_KEY`.

---

## MCP server

`dq mcp` starts a read-only [MCP](https://modelcontextprotocol.io) server, usable from Claude Desktop or Claude Code: `list_tables`, `describe_table`, `source_contract` (gap between the contract and the actual schema), `test_results` (last `dbt build`), `failing_rows`, `column_profile`, `decisions` (agent log) and `quarantine`.

```json
{"mcpServers": {"dq-agent": {"command": "uv", "args": ["run", "--directory", "/path/to/dq-agent", "dq", "mcp"],
                             "env": {"PYTHONPATH": "/path/to/dq-agent/src"}}}}
```

---

## Project structure

```text
dbt/                    dbt project: sources (contract), staging, marts, custom tests
src/dq_agent/
  ingest.py             OpenSky + OurAirports loading, pseudonymisation, clean baseline
  anomalies.py          8 reproducible anomaly scenarios and their ground truth
  dbt_runner.py         dbt build, run_results.json and lineage parsing
  context.py            read-only context tools
  reasoners.py          LLM reasoner (typed actions or free SQL) and rule-based reasoner
  actions.py            typed fix actions and SQL generation
  guardrails.py         SQL checks, dry run, quarantine, apply
  graph.py              LangGraph graph and human review loop
  review.py             reviewers: terminal, simulated (evaluation)
  evaluate.py           measured evaluation
  mcp_server.py         read-only MCP server
tests/                  47 pytest tests
reports/                evaluation results (JSONL) and RESULTS.md
```

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

The code is released under the [MIT License](LICENSE). OpenSky data remains subject to the OpenSky Network licence; OurAirports is in the public domain.

## Authors

<a href="https://github.com/Yvngval"><img src="https://github.com/Yvngval.png?size=96" width="64" height="64" alt="Yvngval"></a>

Valentin Ponnoussamy ([@Yvngval](https://github.com/Yvngval)), Master's in Data, Knowledge and Intelligence, Université Paris Cité, 2026.
