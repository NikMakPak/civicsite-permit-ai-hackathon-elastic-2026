"""Normalize six heterogeneous NYC DOB datasets into one provenance-carrying record schema.

Entity resolution keys: BIN (building) and BBL (tax lot). BBL is rebuilt from boro/block/lot where a
dataset does not carry it, so rows can be joined by either key. Every record keeps a verifiable
`source_url` that re-fetches the exact source row from NYC Open Data.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import quote

from .socrata import DATASETS, KEY_FIELD

BORO_CODE = {"manhattan": "1", "bronx": "2", "brooklyn": "3", "queens": "4", "staten island": "5"}
WWP_RE = re.compile(r"WITHOUT\s+(A\s+)?PERMIT|W/O\s+PERMIT|NO\s+PERMIT|WORK\s+WITHOUT|FAILURE\s+TO\s+OBTAIN\s+PERMIT", re.I)
PERIODIC_RE = re.compile(r"FAILURE TO FILE|FTF-|FTC-|LL84|LL152|LL87|BENCHMARK|AEUHAZ|LBLVIO", re.I)
STOP_WORK_RE = re.compile(r"STOP\s+WORK|SWO\b|VACATE", re.I)

# ---------------------------------------------------------------- generic cleaners


def clean(v: Any) -> str | None:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s or None


def to_date(v: Any) -> str | None:
    """Parse ISO, YYYYMMDD or MM/DD/YYYY into 'YYYY-MM-DD'; return None for junk (e.g. 'Y9990120')."""
    s = clean(v)
    if not s:
        return None
    try:
        if re.fullmatch(r"\d{8}", s):
            d = datetime.strptime(s, "%Y%m%d").date()
        elif re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", s):
            d = datetime.strptime(s, "%m/%d/%Y").date()
        else:
            d = datetime.fromisoformat(s[:10]).date()
    except ValueError:
        return None
    if not (1990 <= d.year <= 2100):
        return None
    return d.isoformat()


def to_float(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def pad_bin(v: Any) -> str | None:
    s = clean(v)
    if not s or not s.isdigit() or int(s) == 0:
        return None
    return s


def make_bbl(boro: str | None, block: Any, lot: Any, bbl_field: Any = None) -> str | None:
    raw = clean(bbl_field)
    if raw:
        raw = raw.split(".")[0]
        if raw.isdigit() and len(raw) == 10:
            return raw
    b, bl, lt = clean(boro), clean(block), clean(lot)
    if b and bl and lt and bl.isdigit() and lt.isdigit():
        b = BORO_CODE.get(b.lower(), b)
        if b.isdigit():
            return f"{b}{int(bl):05d}{int(lt):04d}"
    return None


def make_address(house: Any, street: Any) -> str | None:
    h, st = clean(house), clean(street)
    if not st:
        return None
    return f"{h} {st}".upper() if h else st.upper()


def source_url(dataset: str, key: str) -> str:
    ds = DATASETS[dataset]
    return f"https://data.cityofnewyork.us/resource/{ds.dataset_id}.json?{KEY_FIELD[dataset]}={quote(key)}"


def _loc(row: dict) -> dict | None:
    lat, lon = to_float(row.get("latitude")), to_float(row.get("longitude"))
    if lat is None or lon is None or not (40 < lat < 41.5) or not (-75 < lon < -73):
        return None
    return {"lat": lat, "lon": lon}


# ---------------------------------------------------------------- status vocabularies

FILING_GROUPS = {
    "objections": {"Objections", "Incomplete", "QA Failed", "LL 158-2017-Denied"},
    "in_review": {
        "Plan Examiner Review", "Pending Plan Examiner Assignment",
        "Chief Plan Examiner/ Assistant Chief Plan Examiner Review", "Pending CPE/ACPE Assignment",
        "SO Plan Examiner Review", "Prof Cert QA Review", "Pending Prof Cert QA Assignment",
        "Awaiting Energy Approval",
    },
    "approved": {"Approved", "PAA Approved"},
    "permitted": {"Permit Entire", "Permit Issued"},
    "withdrawn": {"Filing Withdrawn"},
    "closed": {
        "LOC Issued", "CO Issued", "TA Certificate of Operation Issued",
        "PA Certificate of Operation Issued", "Full Demolition Signed-off",
    },
}


def filing_group(status: str | None) -> str:
    if not status:
        return "other"
    for g, names in FILING_GROUPS.items():
        if status in names:
            return g
    if status.lower().startswith("on hold") or status.lower().startswith("onhold"):
        return "on_hold"
    return "other"


def _base(dataset: str, key: str, rtype: str) -> dict:
    return {
        "record_id": f"{dataset}:{key}",
        "source_dataset": dataset,
        "source_dataset_id": DATASETS[dataset].dataset_id,
        "source_key": key,
        "source_url": source_url(dataset, key),
        "record_type": rtype,
        "is_wwp": False,
        "data_quality": None,
        "violation_class": "construction_safety" if rtype == "violation" else None,
    }


# ---------------------------------------------------------------- per-dataset normalizers


def norm_filing(r: dict, now_iso: str) -> dict | None:
    key = clean(r.get("job_filing_number"))
    if not key:
        return None
    d = _base("filings", key, "filing")
    status = clean(r.get("filing_status"))
    applicant = clean(r.get("applicant_business_name")) or clean(
        f"{r.get('applicant_first_name') or ''} {r.get('applicant_last_name') or ''}"
    )
    addr = make_address(r.get("house_no"), r.get("street_name"))
    d.update(
        bin=pad_bin(r.get("bin")),
        bbl=make_bbl(r.get("borough"), r.get("block"), r.get("lot"), r.get("bbl")),
        borough=clean(r.get("borough")),
        address=addr,
        zip=clean(r.get("postcode")),
        job_filing_number=key,
        status=status,
        status_group=filing_group(status),
        status_date=to_date(r.get("current_status_date")),
        event_date=to_date(r.get("filing_date")),
        filing_date=to_date(r.get("filing_date")),
        approved_date=to_date(r.get("approved_date")),
        first_permit_date=to_date(r.get("first_permit_date")),
        signoff_date=to_date(r.get("signoff_date")),
        work_type=clean(r.get("job_type")),
        summary=f"{clean(r.get('job_type')) or 'Job'} filing {key} - {status} ({addr or 'no address'})",
        description=clean(r.get("job_description")),
        cost=to_float(r.get("initial_cost")),
        applicant=applicant,
        owner=clean(r.get("owner_s_business_name")),
        filing_rep=clean(r.get("filing_representative_business_name")),
        location=_loc(r),
    )
    return d


def norm_permit(r: dict, now_iso: str) -> dict | None:
    key = clean(r.get("tracking_number")) or clean(r.get("work_permit"))
    if not key:
        return None
    d = _base("permits", key, "permit")
    jfn = clean(r.get("job_filing_number"))
    placeholder = bool(jfn and jfn.lower().startswith("permit"))
    wp = clean(r.get("work_permit"))
    if placeholder:
        # DOB NOW publishes ~300 rows whose job_filing_number is the literal "Permit is no(t yet issued)".
        jfn, wp = None, None
        d["data_quality"] = "placeholder_permit_row"
    status = clean(r.get("permit_status"))
    addr = make_address(r.get("house_no"), r.get("street_name"))
    issued, approved = to_date(r.get("issued_date")), to_date(r.get("approved_date"))
    d.update(
        bin=pad_bin(r.get("bin")),
        bbl=make_bbl(r.get("borough"), r.get("block"), r.get("lot"), r.get("bbl")),
        borough=(clean(r.get("borough")) or "").title() or None,
        address=addr,
        zip=clean(r.get("zip_code")),
        job_filing_number=jfn,
        permit_number=wp,
        status=status,
        status_group={"Permit Issued": "active", "Signed-off": "closed"}.get(status or "", "other"),
        status_date=issued or approved,
        event_date=issued or approved,
        approved_date=approved,
        issued_date=issued,
        expired_date=to_date(r.get("expired_date")),
        work_type=clean(r.get("work_type")),
        summary=f"{clean(r.get('work_type')) or 'Work'} permit {wp or key} - {status}",
        description=clean(r.get("job_description")),
        cost=to_float(r.get("estimated_job_costs")),
        applicant=clean(r.get("applicant_business_name")),
        owner=clean(r.get("owner_business_name")),
        location=_loc(r),
    )
    return d


def norm_safety(r: dict, now_iso: str) -> dict | None:
    key = clean(r.get("violation_number"))
    if not key:
        return None
    d = _base("safety_violations", key, "violation")
    status = clean(r.get("violation_status"))
    vtype, device = clean(r.get("violation_type")), clean(r.get("device_type"))
    remarks = clean(r.get("violation_remarks"))
    text = " - ".join(x for x in (device, vtype, remarks) if x)
    addr = make_address(r.get("house_number"), r.get("street"))
    issue = to_date(r.get("violation_issue_date"))
    d.update(
        bin=pad_bin(r.get("bin")),
        bbl=make_bbl(r.get("borough"), r.get("block"), r.get("lot"), r.get("bbl")),
        borough=clean(r.get("borough")),
        address=addr,
        zip=clean(r.get("zip")),
        violation_number=key,
        status=status,
        status_group="open" if (status or "").lower() == "active" else "closed",
        status_date=issue,
        event_date=issue,
        work_type=device,
        severity=None,
        summary=f"DOB Safety violation {key} ({device or 'device'}) - {status}",
        description=text or None,
        is_wwp=bool(WWP_RE.search(text)),
        violation_class="periodic_compliance" if (PERIODIC_RE.search(text) or (device or "") in (
            "Elevators", "Boiler", "Gas Piping", "Benchmarking")) else "construction_safety",
        location=_loc(r),
    )
    return d


def norm_legacy(r: dict, now_iso: str) -> dict | None:
    key = clean(r.get("number")) or clean(r.get("violation_number"))
    if not key:
        return None
    d = _base("legacy_violations", key, "violation")
    cat = clean(r.get("violation_category")) or ""
    up = cat.upper()
    if up.endswith("ACTIVE") or up.endswith("- ACTIVE"):
        sg = "open"
    elif "DISMISSED" in up or "RESOLVED" in up:
        sg = "closed"
    else:
        sg = "unknown"
    vtype = clean(r.get("violation_type"))
    text = " - ".join(x for x in (vtype, clean(r.get("description")), clean(r.get("disposition_comments"))) if x)
    addr = make_address(r.get("house_number"), r.get("street"))
    issue = to_date(r.get("issue_date"))
    d.update(
        bin=pad_bin(r.get("bin")),
        bbl=make_bbl(r.get("boro"), r.get("block"), r.get("lot")),
        borough="Manhattan" if clean(r.get("boro")) == "1" else clean(r.get("boro")),
        address=addr,
        violation_number=clean(r.get("violation_number")) or key,
        status=cat or None,
        status_group=sg,
        status_date=to_date(r.get("disposition_date")) or issue,
        event_date=issue,
        work_type=vtype,
        summary=f"DOB violation {key} - {cat}",
        description=text or None,
        is_wwp=("WORK W" in up) or bool(WWP_RE.search(text)),
    )
    if up.startswith("VP") and clean(r.get("ecb_number")):
        # "unserved ECB" rows mirror an ECB summons that also appears in the ECB dataset -> avoid double counting
        d["data_quality"] = "ecb_mirror"
        d["linked_ecb"] = clean(r.get("ecb_number"))
    return d


def norm_ecb(r: dict, now_iso: str) -> dict | None:
    key = clean(r.get("ecb_violation_number"))
    if not key:
        return None
    d = _base("ecb_violations", key, "violation")
    status = clean(r.get("ecb_violation_status"))
    text = " | ".join(
        x for x in (clean(r.get("violation_description")), clean(r.get("section_law_description1"))) if x
    )
    issue = to_date(r.get("issue_date"))
    sev = clean(r.get("severity"))
    d.update(
        bin=pad_bin(r.get("bin")),
        bbl=make_bbl(r.get("boro"), r.get("block"), r.get("lot")),
        borough="Manhattan" if clean(r.get("boro")) == "1" else clean(r.get("boro")),
        address=None,  # ECB 'respondent_*' is the owner's mailing address, not the site; filled from BIN map
        violation_number=key,
        status=status,
        status_group="open" if (status or "").upper() == "ACTIVE" else "closed",
        status_date=issue,
        event_date=issue,
        severity=sev,
        work_type=clean(r.get("violation_type")),
        summary=f"ECB violation {key} ({sev or 'unclassified'}) - {status}, hearing {clean(r.get('hearing_status')) or 'n/a'}",
        description=text or None,
        cost=to_float(r.get("balance_due")),
        is_wwp=bool(WWP_RE.search(text)),
        owner=clean(r.get("respondent_name")),
        linked_dob_violation=clean(r.get("dob_violation_number")),
    )
    return d


def norm_complaint(r: dict, now_iso: str) -> dict | None:
    key = clean(r.get("complaint_number"))
    if not key:
        return None
    d = _base("complaints", key, "complaint")
    status = clean(r.get("status"))
    code = clean(r.get("complaint_category"))
    addr = make_address(r.get("house_number"), r.get("house_street"))
    entered = to_date(r.get("date_entered"))
    d.update(
        bin=pad_bin(r.get("bin")),
        borough="Manhattan",
        address=addr,
        zip=clean(r.get("zip_code")),
        status=status,
        status_group="open" if (status or "").upper() == "ACTIVE" else "closed",
        status_date=to_date(r.get("disposition_date")) or entered,
        event_date=entered,
        work_type=f"complaint_category_{code}" if code else None,
        summary=f"DOB complaint {key} (category {code}) - {status}",
        description=f"DOB complaint category {code}; disposition {clean(r.get('disposition_code')) or 'n/a'}",
    )
    return d


NORMALIZERS = {
    "filings": norm_filing,
    "permits": norm_permit,
    "safety_violations": norm_safety,
    "legacy_violations": norm_legacy,
    "ecb_violations": norm_ecb,
    "complaints": norm_complaint,
}


def normalize_all(raw: dict[str, list[dict]]) -> list[dict]:
    """Normalize every raw row, drop unusable ones, fill derived fields, dedupe by record_id."""
    now_iso = datetime.now(timezone.utc).isoformat()
    out: dict[str, dict] = {}
    for name, rows in raw.items():
        fn = NORMALIZERS[name]
        for r in rows:
            rec = fn(r, now_iso)
            if rec is None or not rec.get("bin"):
                continue  # BIN is the primary join key; BIN-less rows cannot be resolved to a building
            rec["ingested_at"] = now_iso
            out[rec["record_id"]] = rec

    # Fill missing site addresses / borough / BBL from any record of the same BIN.
    by_bin: dict[str, dict] = {}
    for rec in out.values():
        if rec.get("address") and rec["bin"] not in by_bin:
            by_bin[rec["bin"]] = rec
    for rec in out.values():
        ref = by_bin.get(rec["bin"])
        if ref:
            rec["address"] = rec.get("address") or ref["address"]
            rec["bbl"] = rec.get("bbl") or ref.get("bbl")
            rec["zip"] = rec.get("zip") or ref.get("zip")
            rec["location"] = rec.get("location") or ref.get("location")
    return list(out.values())


# ---------------------------------------------------------------- semantic-field budgeting

OPEN_FILING_GROUPS = {"objections", "on_hold", "in_review", "approved"}


def assign_semantic_text(records: list[dict], limit: int, boost_ids: frozenset[str] | set[str] = frozenset()) -> int:
    """Embed only the text that matters (open blockers, open violations), newest first, up to `limit`.

    Embedding ~165k mostly-closed rows would burn quota for no demo value; the BM25 side of the
    hybrid query still covers every record's description.
    """

    def priority(r: dict) -> int:
        if not r.get("description") or len(r["description"]) < 15:
            return 0
        if r["record_id"] in boost_ids:  # records cited by a flag are embedded first
            return 5
        if r["record_type"] == "filing" and r["status_group"] in OPEN_FILING_GROUPS:
            return 3
        if r["record_type"] == "violation" and r["status_group"] == "open":
            return 3
        if r["record_type"] == "permit" and r["status_group"] == "active":
            return 1
        return 0

    cands = [r for r in records if priority(r) > 0]
    cands.sort(key=lambda r: (priority(r), r.get("event_date") or ""), reverse=True)
    n = 0
    for r in cands[:limit]:
        r["description_semantic"] = r["description"][:900]
        n += 1
    return n


def as_date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None
