# CivicSite Permit Copilot: PRD v2 (corrected after data audit)

Based on the original CivicSite research/PRD. This version keeps the wedge (NYC DOB preflight + evidence for small AEC teams) and fixes everything that did not survive contact with the live data on 2026-10-07.

## Corrections to the original plan

| # | Original assumption | What the live data shows | Fix in the product |
|---|---|---|---|
| 1 | Use legacy Job Application Filings (`ic3t-wcy2`) and Permit Issuance (`ipu4-2q9a`) plus DOB NOW | Both legacy sets are **frozen**: latest action 2020-05-21 / issuance 2020-06-05. A "stale action" rule on them flags everything. | Use only live sets: DOB NOW Filings `w9ak-ipjd`, DOB NOW Approved Permits `rbx6-tga4`, DOB Safety Violations `855j-jady`, DOB Violations `3h2n-5cm9`, ECB `6bgk-3dad`, Complaints `eabe-havv`. |
| 2 | Join DOB Violations "by BIN" | `3h2n-5cm9` has BIN on every row since 2015 (the oldest rows lack it, which is easy to misread from a 2-row sample). BBL is still absent, only boro/block/lot. | Join on BIN; **rebuild BBL** from boro+block+lot (zero-padded 1+5+4) so BIN and BBL both resolve. |
| 3 | Permits dataset borough filter | `rbx6-tga4.borough` is mixed case (`Manhattan` 45,974 rows vs `MANHATTAN` 19,074 in-window). A naive filter silently drops 70% of permits. | `upper(borough)='MANHATTAN'`. |
| 4 | Permit `job_filing_number` is a clean join key | 326 rows carry the literal placeholder "Permit is no(t yet issued)". | Tagged `data_quality=placeholder_permit_row`, excluded from rules. |
| 5 | DOB dates are dates | DOB Violations uses `YYYYMMDD` text with junk (`Y9990120`), ECB the same, complaints `MM/DD/YYYY`, Safety Violations ISO. | One tolerant parser; junk becomes null, year outside 1990-2100 rejected. Test records (`house_number='TEST'`) filtered. |
| 6 | "Filing-permit gap: filing exists without approved permit" | Applied naively it flags 18,407 of 59,008 filings. 15,935 of the "Approved" ones are `-P#` post-approval amendments, which never carry their own permit; `No Work` and `Alteration CO` don't need one either. | Rule restricted to `-I`/`-S` filings of permit-bearing job types; severity capped at *medium* (ready-to-issue = applicant action), *low* when dormant >180 days. |
| 7 | "Stale action = latest action date is old" | Meaningless on a mixed corpus. The informative signal is **who owns the wait**: `Objections`, `Incomplete`, `QA Failed` are applicant-owned (the 70% in the NYC Comptroller audit); `Plan Examiner Review` queues are DOB-owned. | Every flag carries `controlled_by: applicant | agency | shared`. Applicant-owned flags rank higher; agency waits are *track-only*. |
| 8 | Legacy violations and ECB are separate truths | "Unserved ECB" legacy rows (`VP*` categories) mirror ECB summonses that also appear in the ECB set. | Tagged `ecb_mirror`, not double counted. |
| 9 | ECB has an address | ECB `respondent_*` is the **owner's mailing address**, not the site. | Site address filled from any other record of the same BIN. |
| 10 | Embed everything | ~270K rows in the slice; most are closed. | `semantic_text` only on open blockers, open violations, active permits and any record cited by a flag (default cap 20,000). BM25 covers the rest. |
| 11 | "Citation accuracy" as a slide claim | Needs a procedure. | `civicsite eval`: re-reads each cited row live from Socrata and checks existence, BIN and status/date; exports a labeling CSV for precision. |

## Product (unchanged core)

Backend-first preflight for small NYC architecture / GC / expediting teams. Input: address, BIN or BBL. Output: unified timeline and a **ranked, cited action queue**. It never issues compliance verdicts.

### Trust model
- Mistral extracts and routes (structured output, function calling).
- Elasticsearch finds and stores provenance (hybrid BM25 + `semantic_text`, filters, aggregations).
- Python rules decide what is flagged and compute every interval.
- A human decides.

### Rules (reason codes)
`OBJECTIONS_AWAITING_RESPONSE`, `ON_HOLD_BLOCKER`, `APPROVED_NO_PERMIT`, `CHRONOLOGY_CONFLICT`, `PERMIT_VIOLATION_CONFLICT`, `WWP_NO_ACTIVE_PERMIT`, `PERMIT_EXPIRING`, `STALE_AGENCY_QUEUE`. Each flag has severity, score, controlled_by, join basis (job number / BIN / permit number), confidence, caveat and evidence rows with a re-fetchable `source_url`.

### Scope of the hackathon MVP
Manhattan; filings from the last 12 months, violations and complaints from the last 24. No drawing review, no code compliance, no legal advice.

## Hackathon criteria mapping

| Criterion | How CivicSite answers it |
|---|---|
| Novelty | Cross-dataset *evidence graph with ownership of delay* (applicant vs agency), not another Q&A bot; honest data-quality audit; verifiable citations. |
| Use of Elastic | Serverless; explicit mappings; `semantic_text` on a Mistral inference endpoint; RRF hybrid retriever; term/range filters; aggregations (portfolio); ES\|QL tools; Agent Builder agent + MCP; evidence cache index. |
| Use of Mistral | `mistral-embed` registered *inside* Elasticsearch; chat model via function calling over Elastic tools; structured output (Pydantic) with verbatim-quote verification; Mistral chat endpoint registered in Elastic for Agent Builder. |

## Product requirements for the startup path

- **Customer:** NYC small architecture / GC / expediter teams with 5-50 live filings; economic buyer = principal.
- **Wedge value:** fewer missed follow-ups and shorter applicant-controlled time, not "AI permits".
- **Pricing hypotheses to test (unchanged):** $49 solo, $149-199 team, $25-75 per project.
- **Trust requirements before sale:** citation correctness >=95% and high-severity precision >=85% on a labeled set (harness included).
- **Next (30 days):** correction-letter PDF upload with Mistral OCR, comment-response matrix, scheduled refresh (Socrata `$where` on `current_status_date`), email digest.
- **Later:** data-center entitlement evidence layer on hearing notices/ordinances; one more AHJ.

## Open risks
- Absence-of-evidence flags (`APPROVED_NO_PERMIT`) depend on dataset freshness; flagged with a caveat.
- BIN-level association (permit vs violation) cannot prove same scope; confidence 0.7 and caveat shown.
- Complaint category codes are shown raw (no official lookup was embedded).
