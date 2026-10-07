# CivicSite Permit Copilot

**A cited, version-aware action queue over fragmented NYC Department of Buildings records, for small architecture, GC and expediting teams.**
Built for the Elastic × Mistral NYC Hack Night (2026-10-07).

> Input an address, BIN or BBL → get one unified timeline of DOB NOW filings, permits, violations and complaints, plus a ranked list of what is blocking the job, who owns the delay (applicant vs DOB), what to do next, and the exact source row for every claim.

## Architecture

```
NYC Open Data (Socrata, 6 live DOB datasets)
        │  fetch → normalize (BIN/BBL entity resolution, tolerant dates, data-quality tags)
        ▼
 deterministic checks (Python)  ──►  flags + project risk scores
        │
        ▼
 Elasticsearch Serverless
   civicsite_records   BM25 + semantic_text  ← Mistral `mistral-embed` registered as an Elastic inference endpoint
   civicsite_flags     cited flags (nested evidence with source_url)
   civicsite_projects  one doc per building, risk score, aggregations
   civicsite_evidence  cache of Mistral structured extractions
        ▲                          ▲
        │ RRF hybrid / filters     │ ES|QL tools, index_search tool, MCP
 Mistral function-calling agent    Elastic Agent Builder agent "civicsite-copilot"
        │
 FastAPI + one-page UI + CLI
```

**Trust model:** Mistral extracts and routes · Elasticsearch finds and stores provenance · Python rules decide what is flagged and compute intervals · a human decides. After every agent answer the `[record_id]` citations are verified against what the tools actually returned.

| | What | Where |
|---|---|---|
| Elastic | explicit mappings, `semantic_text`, RRF retriever (BM25 + semantic), filters, aggregations, ES\|QL & index_search tools in Agent Builder, MCP endpoint | `es_setup.py`, `store.py`, `agent_builder.py` |
| Mistral | embeddings (inside Elastic), chat model with function calling over Elastic tools, Pydantic structured output with verbatim-quote verification, chat inference endpoint for Agent Builder | `agent.py`, `extract.py`, `es_setup.py` |

## Quick start

```powershell
cd civicsite
uv venv .venv ; uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
copy .env.example .env     # fill ELASTICSEARCH_URL, ELASTIC_API_KEY, KIBANA_URL, MISTRAL_API_KEY

civicsite fetch            # 1. ~230k rows from NYC Open Data -> data/raw   (~1.5 min)
civicsite build            # 2. normalize + run rules -> data/processed      (~1 min)
civicsite setup --recreate # 3. Mistral inference endpoints + indices in Elastic
civicsite index            # 4. bulk index (Mistral embeds semantic_text at ingest)
civicsite agent-builder    # 5. (bonus) register tools + agent in Kibana Agent Builder; prints MCP URL
civicsite serve            # 6. http://127.0.0.1:8000
```

No Elastic yet? `civicsite check "439 East 77 Street" --backend local` and `civicsite serve --backend local` run fully offline (lexical search only).

### Demo commands

```powershell
civicsite check "439 East 77 Street"
civicsite search "unauthorized conversion of apartments" --record-type violation   # meaning-based
civicsite ask "Which buildings have the most unanswered objections?"
civicsite ask "What is blocking 439 East 77 Street and who has to act?"
civicsite eval --n 100                      # citation correctness vs live NYC Open Data + labeling CSV
civicsite eval --labels eval/labels.csv     # precision per reason code after you label
pytest
```

## Data (all live as of 2026-10-07)

| Dataset | Socrata id | Slice |
|---|---|---|
| DOB NOW: Build – Job Application Filings | `w9ak-ipjd` | Manhattan, filed ≥ 12 months ago |
| DOB NOW: Build – Approved Permits | `rbx6-tga4` | Manhattan (case-insensitive), issued/approved ≥ 12 months |
| DOB Safety Violations | `855j-jady` | Manhattan, 24 months |
| DOB Violations (legacy BIS, still updated) | `3h2n-5cm9` | boro 1, 24 months, test rows removed |
| DOB ECB Violations | `6bgk-3dad` | boro 1, 24 months |
| DOB Complaints Received | `eabe-havv` | BIN 1xxxxxx, 2024-2026 |

Intentionally **not** used: legacy BIS Job Filings and Permit Issuance (frozen in mid-2020). See [docs/PRD_v2.md](docs/PRD_v2.md) for the full data audit and what changed versus the original PRD.

## Rules

`OBJECTIONS_AWAITING_RESPONSE` · `ON_HOLD_BLOCKER` · `APPROVED_NO_PERMIT` · `CHRONOLOGY_CONFLICT` · `PERMIT_VIOLATION_CONFLICT` · `WWP_NO_ACTIVE_PERMIT` · `PERMIT_EXPIRING` · `STALE_AGENCY_QUEUE`. Every flag has severity, `controlled_by` (applicant / agency / shared), join basis, confidence, caveat and evidence with a re-fetchable `source_url`. Flags are *follow-up candidates*, not legal or code-compliance verdicts.

## Limits (be honest in the demo)

- Manhattan, last 12 months of filings; no drawing review.
- Absence-of-evidence flags depend on dataset freshness; BIN-level links cannot prove the same scope (confidence 0.7, caveat shown).
- Complaint categories are shown as raw DOB codes.
- Precision numbers require human labels (`eval/labels_template.csv`); only citation correctness is fully automatic.
