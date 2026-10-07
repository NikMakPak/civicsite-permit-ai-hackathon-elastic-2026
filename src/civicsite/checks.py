"""Deterministic preflight checks.

Trust model: the LLM never decides whether something is wrong. These rules do, using dates and
statuses from the normalized records; every flag cites the exact source rows that triggered it.

Reason codes
  OBJECTIONS_AWAITING_RESPONSE  filing sits in Objections/Incomplete/QA Failed - applicant owes a response
  ON_HOLD_BLOCKER               filing on hold (administrative or supersede request)
  APPROVED_NO_PERMIT            filing approved but no work permit issued/recorded (filing-permit gap)
  CHRONOLOGY_CONFLICT           permit/filing dates or statuses contradict each other
  PERMIT_VIOLATION_CONFLICT     active permit while open violation(s) on the same building
  WWP_NO_ACTIVE_PERMIT          open work-without-permit violation and no active permit on the building
  PERMIT_EXPIRING               active permit expired in the last 30 days or expires within 30 days
  PERIODIC_FILING_OVERDUE       open failure-to-file periodic violations (elevator/boiler/gas/LL84) - low priority
  STALE_AGENCY_QUEUE            filing waiting in a DOB-controlled review queue for 60+ days (track-only)
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date, datetime, timezone

from .normalize import STOP_WORK_RE, as_date

SEV_WEIGHT = {"high": 70, "medium": 40, "low": 15}
NO_PERMIT_NEEDED = ("No Work", "Alteration CO", "ALT-CO")
HIGH_ECB = {"CLASS - 1", "Hazardous"}


def _is_permit_bearing(job_filing_number: str | None) -> bool:
    """-I (initial) and -S (subsequent) filings carry permits; -P (post-approval amendment) and -A/-B/... do not."""
    if not job_filing_number or "-" not in job_filing_number:
        return False
    return job_filing_number.rsplit("-", 1)[1][:1] in ("I", "S")


def days_between(a: str | None, b: date) -> int | None:
    d = as_date(a)
    return (b - d).days if d else None


def _ev(rec: dict, note: str | None = None) -> dict:
    return {
        "record_id": rec["record_id"],
        "source_dataset": rec["source_dataset"],
        "source_dataset_id": rec["source_dataset_id"],
        "source_key": rec["source_key"],
        "source_url": rec["source_url"],
        "record_type": rec["record_type"],
        "date": rec.get("event_date"),
        "status": rec.get("status"),
        "summary": rec.get("summary"),
        "note": note,
    }


def _flag(bin_: str, anchor: dict, code: str, title: str, severity: str, controlled_by: str,
          days: int | None, explanation: str, action: str, owner: str, evidence: list[dict],
          confidence: float, basis: str, caveat: str | None, today: date) -> dict:
    score = SEV_WEIGHT[severity] + min(max(days or 0, 0), 180) / 180 * 20 + (5 if controlled_by == "applicant" else 0)
    fid = hashlib.sha1(f"{bin_}|{code}|{anchor['record_id']}".encode()).hexdigest()[:16]
    return {
        "flag_id": fid,
        "bin": bin_,
        "bbl": anchor.get("bbl"),
        "address": anchor.get("address"),
        "reason_code": code,
        "title": title,
        "severity": severity,
        "score": round(score, 1),
        "controlled_by": controlled_by,
        "days": days,
        "explanation": explanation,
        "recommended_action": action,
        "owner_hint": owner,
        "evidence": evidence,
        "confidence": confidence,
        "join_basis": basis,
        "caveat": caveat,
        "as_of": today.isoformat(),
    }


def run_checks(records: list[dict], today: date) -> list[dict]:
    """Run all checks for the records of ONE building (same BIN)."""
    if not records:
        return []
    bin_ = records[0]["bin"]
    filings = [r for r in records if r["record_type"] == "filing"]
    permits = [r for r in records if r["record_type"] == "permit" and r.get("data_quality") != "placeholder_permit_row"]
    violations = [r for r in records if r["record_type"] == "violation" and r.get("data_quality") != "ecb_mirror"]
    open_all = [v for v in violations if v["status_group"] == "open"]
    # failure-to-file periodic compliance (elevator, boiler, gas piping, LL84) is not construction-scope evidence
    periodic = [v for v in open_all if v.get("violation_class") == "periodic_compliance"]
    open_viol = [v for v in open_all if v.get("violation_class") != "periodic_compliance"]
    permits_by_job: dict[str, list[dict]] = defaultdict(list)
    for p in permits:
        if p.get("job_filing_number"):
            permits_by_job[p["job_filing_number"]].append(p)
    active_permits = [
        p for p in permits
        if p["status_group"] == "active" and (not p.get("expired_date") or as_date(p["expired_date"]) >= today)
    ]
    flags: list[dict] = []

    for f in filings:
        g, age = f["status_group"], days_between(f.get("status_date"), today)
        jfn = f["job_filing_number"]

        if g == "objections" and age is not None and age >= 14:
            sev = "high" if age >= 45 else "medium"
            flags.append(_flag(
                bin_, f, "OBJECTIONS_AWAITING_RESPONSE",
                f"Filing {jfn} has been in '{f['status']}' for {age} days",
                sev, "applicant", age,
                f"DOB NOW shows status '{f['status']}' since {f['status_date']}. The applicant of record "
                "must answer the examiner's objections / complete the filing before review can continue.",
                "Open the filing in DOB NOW, list each objection, assign an owner and due date, resubmit "
                "corrected documents, and log the response.",
                "Applicant of record / project architect", [_ev(f, f"status since {f['status_date']}")],
                0.95, "job_filing_number", None, today))

        if g == "on_hold" and age is not None and age >= 7:
            applicant_side = any(k in f["status"] for k in ("Supersede", "Withdrew", "Required"))
            flags.append(_flag(
                bin_, f, "ON_HOLD_BLOCKER",
                f"Filing {jfn} on hold ({f['status']}) for {age} days",
                "high" if age >= 30 else "medium", "applicant" if applicant_side else "shared", age,
                f"Status '{f['status']}' since {f['status_date']} blocks progress on this filing.",
                "Identify who must act (applicant, owner, special/progress inspector or DOB) and clear the "
                "hold request in DOB NOW.",
                "Permit coordinator", [_ev(f, f"on hold since {f['status_date']}")],
                0.9, "job_filing_number", None, today))

        if g == "approved" and _is_permit_bearing(jfn) and not f.get("first_permit_date")                 and not permits_by_job.get(jfn) and not (f.get("work_type") or "").startswith(NO_PERMIT_NEEDED):
            a = days_between(f.get("approved_date") or f.get("status_date"), today)
            if a is not None and 30 <= a:
                dormant = a > 180
                flags.append(_flag(
                    bin_, f, "APPROVED_NO_PERMIT",
                    (f"Filing {jfn} approved {a} days ago, ready to issue but no work permit requested"
                     if not dormant else
                     f"Filing {jfn} approved {a} days ago and still has no work permit (dormant?)"),
                    "low" if dormant else "medium", "applicant", a,
                    f"The filing is '{f['status']}' (approved {f.get('approved_date') or f['status_date']}) with "
                    "no first_permit_date and no row in DOB NOW Approved Permits for this job filing number: "
                    "approved, but the applicant has not obtained a work permit.",
                    ("Confirm the contractor of record and request the work permit(s) in DOB NOW."
                     if not dormant else
                     "Confirm whether the job is still planned; if not, withdraw it so it is not mistaken for active work."),
                    "Permit coordinator / GC", [_ev(f, "approved, no matching permit row")],
                    0.8, "job_filing_number",
                    "Applies to initial (-I) and subsequent (-S) filings only; post-approval amendments (-P) never "
                    "carry their own permits. Re-check DOB NOW before acting.",
                    today))

        # --- chronology: job signed off but permit still open
        if f["status_group"] == "closed" and f.get("signoff_date"):
            so = days_between(f["signoff_date"], today)
            open_p = [p for p in permits_by_job.get(jfn, []) if p["status_group"] == "active"]
            if open_p and so is not None and so >= 30:
                flags.append(_flag(
                    bin_, f, "CHRONOLOGY_CONFLICT",
                    f"Job {jfn} signed off {so} days ago but {len(open_p)} permit(s) still show 'Permit Issued'",
                    "low", "shared", so,
                    f"Filing sign-off date {f['signoff_date']} precedes permit status updates; the two datasets disagree.",
                    "Verify permit sign-off in DOB NOW and request correction if the permit should be closed.",
                    "Permit coordinator", [_ev(f, f"signoff {f['signoff_date']}")] + [_ev(p) for p in open_p[:3]],
                    0.85, "job_filing_number", None, today))

        if g in ("in_review",) and age is not None and age >= 60:
            flags.append(_flag(
                bin_, f, "STALE_AGENCY_QUEUE",
                f"Filing {jfn} waiting in DOB queue '{f['status']}' for {age} days",
                "low", "agency", age,
                f"No status change since {f['status_date']}. This queue is DOB-controlled; it is a follow-up "
                "candidate, not a violation.",
                "Track only: ask the assigned examiner / DOB NOW inquiry for an ETA and note the answer.",
                "Permit coordinator", [_ev(f, f"status since {f['status_date']}")],
                0.9, "job_filing_number", "Track-only; applicant cannot clear this state.", today))

    # --- chronology: permit issued before its filing was approved
    filings_by_job = {f["job_filing_number"]: f for f in filings}
    for p in permits:
        f = filings_by_job.get(p.get("job_filing_number") or "")
        if f and p.get("issued_date") and f.get("approved_date") and p["issued_date"] < f["approved_date"]:
            gap = (as_date(f["approved_date"]) - as_date(p["issued_date"])).days
            if gap >= 1:
                flags.append(_flag(
                    bin_, p, "CHRONOLOGY_CONFLICT",
                    f"Permit {p.get('permit_number')} issued {gap} day(s) before filing {f['job_filing_number']} approval date",
                    "medium" if gap > 7 else "low", "shared", None,
                    f"Permit issued {p['issued_date']} but the filing approved_date is {f['approved_date']}; "
                    "dates in the two DOB NOW datasets contradict each other (amendment/re-approval is a common cause).",
                    "Check whether a post-approval amendment reset the approval date; keep the original approval letter on file.",
                    "Permit coordinator", [_ev(p), _ev(f)], 0.85, "job_filing_number", None, today))

    # --- permit x violation
    if active_permits and open_viol:
        earliest = min(as_date(p["issued_date"]) for p in active_permits if p.get("issued_date")) \
            if any(p.get("issued_date") for p in active_permits) else None
        rel = [
            v for v in open_viol
            if v.get("is_wwp") or (earliest and as_date(v["event_date"]) and
                                   (as_date(v["event_date"]) - earliest).days >= -30)
        ]
        if rel:
            hot = [v for v in rel if v.get("severity") in HIGH_ECB or v.get("is_wwp")
                   or STOP_WORK_RE.search(v.get("description") or "")]
            rel.sort(key=lambda v: (v not in hot, v.get("event_date") or ""), reverse=False)
            anchor = hot[0] if hot else rel[0]
            age = days_between(anchor.get("event_date"), today)
            flags.append(_flag(
                bin_, anchor, "PERMIT_VIOLATION_CONFLICT",
                f"{len(rel)} open violation(s) on a building with {len(active_permits)} active permit(s)",
                "high" if hot else "medium", "applicant", age,
                "Open DOB/ECB violations were issued on this BIN around or after permit issuance. Active "
                "permits do not clear violations, and unresolved violations can block sign-off.",
                "Match each violation to the permitted scope, then document cure (certificate of correction / "
                "ECB hearing) before requesting sign-off.",
                "Owner's rep / expediter",
                [_ev(v) for v in rel[:4]] + [_ev(p) for p in active_permits[:2]],
                0.7, "bin",
                "Linked at building (BIN) level only; the violation may concern work outside this permit's scope.",
                today))

    # --- work-without-permit violation, nothing active
    if not active_permits:
        for v in open_viol:
            if v.get("is_wwp"):
                age = days_between(v.get("event_date"), today)
                flags.append(_flag(
                    bin_, v, "WWP_NO_ACTIVE_PERMIT",
                    f"Open work-without-permit violation {v['violation_number']} and no active permit",
                    "high", "applicant", age,
                    "A violation citing work without a permit is open and DOB NOW shows no active permit on "
                    "this building in the ingested window.",
                    "Confirm scope with the owner, decide between legalization filing and restoring conditions, "
                    "and track the ECB/hearing dates.",
                    "Owner's rep / expediter", [_ev(v)], 0.75, "bin",
                    "Only DOB NOW permits in the ingested window are checked; older legacy-BIS permits are not.",
                    today))
                break  # one flag per building is enough

    # --- permit expiry window (renewals appear as extra rows with the same permit number: latest wins)
    latest: dict[str, dict] = {}
    for p in permits:
        k = p.get("permit_number") or p["record_id"]
        if k not in latest or (p.get("issued_date") or "") > (latest[k].get("issued_date") or ""):
            latest[k] = p
    for p in latest.values():
        if p["status_group"] != "active" or not p.get("expired_date"):
            continue
        f = filings_by_job.get(p.get("job_filing_number") or "")
        if f and f["status_group"] == "closed":
            continue
        left = (as_date(p["expired_date"]) - today).days
        if -30 <= left <= 30:
            flags.append(_flag(
                bin_, p, "PERMIT_EXPIRING",
                (f"Permit {p.get('permit_number')} expired {-left} days ago" if left < 0
                 else f"Permit {p.get('permit_number')} expires in {left} days"),
                "high" if left < 0 else "medium", "applicant", max(-left, 0),
                f"Permit expiration date is {p['expired_date']} and the permit is still '{p['status']}'.",
                "Renew the permit in DOB NOW before work continues, or sign it off if work is complete.",
                "Permit coordinator / GC", [_ev(p, f"expires {p['expired_date']}")],
                0.9, "permit_number", None, today))

    if periodic:
        periodic.sort(key=lambda v: v.get("event_date") or "")
        age = days_between(periodic[0].get("event_date"), today)
        kinds = sorted({(v.get("work_type") or "periodic") for v in periodic})
        flags.append(_flag(
            bin_, periodic[0], "PERIODIC_FILING_OVERDUE",
            f"{len(periodic)} open failure-to-file violation(s): {', '.join(kinds)[:80]}",
            "low", "applicant", age,
            "DOB issued violations for overdue periodic filings (elevator, boiler, gas piping, energy benchmarking). "
            "They are compliance-reporting items owned by the building owner, not construction-scope conflicts.",
            "Have the owner's licensed inspector/filer submit the overdue report, then request dismissal; keep the "
            "receipt with the project file.",
            "Owner / building manager", [_ev(v) for v in periodic[:3]], 0.9, "bin",
            "Low priority for permit work; relevant when sign-off or financing requires clean records.", today))

    flags.sort(key=lambda f: -f["score"])
    return flags


def build_project(records: list[dict], flags: list[dict]) -> dict:
    """One summary doc per building, used for portfolio-level ranking."""
    base = next((r for r in records if r.get("address")), records[0])
    counts: dict[str, int] = defaultdict(int)
    for r in records:
        counts[r["record_type"]] += 1
    open_viol = sum(
        1 for r in records
        if r["record_type"] == "violation" and r["status_group"] == "open" and r.get("data_quality") != "ecb_mirror"
        and r.get("violation_class") != "periodic_compliance"
    )
    scores = sorted((f["score"] for f in flags), reverse=True)
    risk = round(sum(s * (1 if i == 0 else 0.25) for i, s in enumerate(scores)), 1)
    last = max((r["event_date"] for r in records if r.get("event_date")), default=None)
    return {
        "bin": base["bin"], "bbl": base.get("bbl"), "address": base.get("address"), "zip": base.get("zip"),
        "location": base.get("location"),
        "n_filings": counts["filing"], "n_permits": counts["permit"], "n_violations": counts["violation"],
        "n_complaints": counts["complaint"], "n_open_violations": open_viol,
        "open_flag_count": len(flags),
        "flag_reasons": sorted({f["reason_code"] for f in flags}),
        "top_reason": flags[0]["reason_code"] if flags else None,
        "top_flag_title": flags[0]["title"] if flags else None,
        "top_record_id": flags[0]["evidence"][0]["record_id"] if flags else None,
        "risk_score": risk,
        "last_activity": last,
    }


def analyze_all(records: list[dict], today: date) -> tuple[list[dict], list[dict]]:
    by_bin: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_bin[r["bin"]].append(r)
    all_flags, projects = [], []
    now = datetime.now(timezone.utc).isoformat()
    for bin_, recs in by_bin.items():
        fl = run_checks(recs, today)
        for f in fl:
            f["computed_at"] = now
        all_flags.extend(fl)
        projects.append(build_project(recs, fl))
    return all_flags, projects


def timeline(records: list[dict], limit: int = 200) -> list[dict]:
    ev = [
        {
            "date": r.get("event_date"), "type": r["record_type"], "dataset": r["source_dataset"],
            "status": r.get("status"), "status_group": r.get("status_group"), "summary": r.get("summary"),
            "record_id": r["record_id"], "source_url": r["source_url"],
        }
        for r in records if r.get("event_date")
    ]
    ev.sort(key=lambda e: e["date"], reverse=True)
    return ev[:limit]
