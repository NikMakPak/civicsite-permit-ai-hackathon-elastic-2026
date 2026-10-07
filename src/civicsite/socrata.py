"""Fetch the NYC Open Data (Socrata) slices CivicSite needs, with on-disk caching.

Datasets (verified live on 2026-10-07):
  w9ak-ipjd  DOB NOW: Build - Job Application Filings   (live, daily)
  rbx6-tga4  DOB NOW: Build - Approved Permits          (live, daily)
  855j-jady  DOB Safety Violations (DOB NOW era)        (live)
  3h2n-5cm9  DOB Violations (legacy BIS; still updated) (live, BIN present on modern rows)
  6bgk-3dad  DOB ECB Violations                         (live)
  eabe-havv  DOB Complaints Received                    (live to ~2025-12-31)

Deliberately NOT used: legacy BIS Job Application Filings (ic3t-wcy2) and Permit Issuance
(ipu4-2q9a) - both frozen in mid-2020, so any "stale" rule on them would flag everything.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .config import RAW_DIR, Settings

BASE = "https://data.cityofnewyork.us/resource"


@dataclass
class Dataset:
    name: str
    dataset_id: str
    select: list[str]
    where: Callable[[Settings], str]


def _ymd(d) -> str:
    return d.strftime("%Y%m%d")


DATASETS: dict[str, Dataset] = {
    "filings": Dataset(
        "filings",
        "w9ak-ipjd",
        [
            "job_filing_number", "bin", "bbl", "borough", "block", "lot", "house_no", "street_name",
            "postcode", "filing_status", "filing_date", "current_status_date", "approved_date",
            "first_permit_date", "signoff_date", "job_type", "job_description", "initial_cost",
            "applicant_business_name", "applicant_first_name", "applicant_last_name",
            "owner_s_business_name", "filing_representative_business_name", "filing_review_type",
            "building_type", "existing_dwelling_units", "proposed_dwelling_units", "latitude", "longitude",
        ],
        lambda s: f"borough='Manhattan' AND filing_date>='{s.filing_start.isoformat()}'",
    ),
    "permits": Dataset(
        "permits",
        "rbx6-tga4",
        [
            "job_filing_number", "work_permit", "sequence_number", "filing_reason", "house_no",
            "street_name", "borough", "block", "lot", "bin", "bbl", "work_type", "permittee_s_license_type",
            "applicant_business_name", "approved_date", "issued_date", "expired_date", "job_description",
            "estimated_job_costs", "owner_business_name", "permit_status", "tracking_number", "zip_code",
            "latitude", "longitude",
        ],
        lambda s: (
            "upper(borough)='MANHATTAN' AND (issued_date>='%s' OR approved_date>='%s')"
            % (s.filing_start.isoformat(), s.filing_start.isoformat())
        ),
    ),
    "safety_violations": Dataset(
        "safety_violations",
        "855j-jady",
        [
            "bin", "violation_issue_date", "violation_number", "violation_type", "violation_remarks",
            "violation_status", "device_number", "device_type", "cycle_end_date", "borough", "block", "lot",
            "house_number", "street", "zip", "latitude", "longitude", "bbl",
        ],
        lambda s: f"borough='Manhattan' AND violation_issue_date>='{s.violation_start.isoformat()}'",
    ),
    "legacy_violations": Dataset(
        "legacy_violations",
        "3h2n-5cm9",
        [
            "isn_dob_bis_viol", "boro", "bin", "block", "lot", "issue_date", "violation_type_code",
            "violation_number", "house_number", "street", "disposition_date", "disposition_comments",
            "device_number", "description", "ecb_number", "number", "violation_category", "violation_type",
        ],
        lambda s: (
            f"boro='1' AND issue_date>='{_ymd(s.violation_start)}' AND issue_date<='{_ymd(s.as_of)}' "
            "AND (house_number is null OR house_number != 'TEST')"
        ),
    ),
    "ecb_violations": Dataset(
        "ecb_violations",
        "6bgk-3dad",
        [
            "ecb_violation_number", "ecb_violation_status", "dob_violation_number", "bin", "boro", "block",
            "lot", "hearing_date", "served_date", "issue_date", "severity", "violation_type",
            "violation_description", "penality_imposed", "balance_due", "infraction_code1",
            "section_law_description1", "aggravated_level", "hearing_status", "respondent_name",
        ],
        lambda s: f"boro='1' AND issue_date>='{_ymd(s.violation_start)}' AND issue_date<='{_ymd(s.as_of)}'",
    ),
    "complaints": Dataset(
        "complaints",
        "eabe-havv",
        [
            "complaint_number", "status", "date_entered", "house_number", "house_street", "zip_code", "bin",
            "complaint_category", "unit", "disposition_date", "disposition_code", "inspection_date",
        ],
        lambda s: (
            "bin like '1%' AND ("
            + " OR ".join(f"date_entered like '%/{y}'" for y in range(s.violation_start.year, s.as_of.year + 1))
            + ")"
        ),
    ),
}


def raw_path(name: str) -> Path:
    return RAW_DIR / f"{name}.jsonl"


def iter_dataset(ds: Dataset, settings: Settings, page: int = 25000) -> Iterator[dict]:
    headers = {"User-Agent": "civicsite-hackathon/0.1 (+https://github.com/)"}
    if settings.socrata_token:
        headers["X-App-Token"] = settings.socrata_token
    where = ds.where(settings)
    offset = 0
    with httpx.Client(timeout=180, headers=headers) as client:
        while True:
            params = {
                "$select": ",".join(ds.select),
                "$where": where,
                "$order": ":id",
                "$limit": page,
                "$offset": offset,
            }
            for attempt in range(5):
                try:
                    r = client.get(f"{BASE}/{ds.dataset_id}.json", params=params)
                    r.raise_for_status()
                    break
                except (httpx.HTTPError, httpx.TimeoutException):
                    if attempt == 4:
                        raise
                    time.sleep(2 * (attempt + 1))
            rows = r.json()
            yield from rows
            if len(rows) < page:
                return
            offset += page


def fetch_dataset(name: str, settings: Settings, refresh: bool = False) -> Path:
    """Download one dataset slice to data/raw/<name>.jsonl (cached)."""
    ds = DATASETS[name]
    path = raw_path(name)
    if path.exists() and not refresh:
        return path
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    n = 0
    with tmp.open("w", encoding="utf-8") as fh:
        for row in iter_dataset(ds, settings):
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    tmp.replace(path)
    return path


def read_raw(name: str) -> list[dict]:
    path = raw_path(name)
    if not path.exists():
        raise FileNotFoundError(f"{path} missing - run `civicsite fetch` first")
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---- live re-verification (used by the evaluation harness) -------------------------------------

KEY_FIELD = {
    "filings": "job_filing_number",
    "permits": "tracking_number",
    "safety_violations": "violation_number",
    "legacy_violations": "number",
    "ecb_violations": "ecb_violation_number",
    "complaints": "complaint_number",
}


def fetch_live_row(dataset: str, key: str, settings: Settings) -> dict | None:
    """Re-read one source row straight from Socrata (source of truth for citation checks)."""
    ds = DATASETS[dataset]
    field = KEY_FIELD[dataset]
    headers = {"User-Agent": "civicsite-hackathon/0.1"}
    if settings.socrata_token:
        headers["X-App-Token"] = settings.socrata_token
    r = httpx.get(
        f"{BASE}/{ds.dataset_id}.json",
        params={field: key, "$limit": 5},
        headers=headers,
        timeout=60,
    )
    r.raise_for_status()
    rows = r.json()
    if dataset == "permits" and len(rows) > 1:
        return rows[0]
    return rows[0] if rows else None
