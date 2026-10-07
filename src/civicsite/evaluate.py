"""Evaluation harness: how much can a user trust a CivicSite flag?

Citation correctness (automatic): for a stratified sample of flags, re-read every cited source row live from
NYC Open Data and verify (a) the row exists, (b) it belongs to the same BIN, (c) the cited status and date still match.
Rows whose status changed since ingestion are reported as `drifted`, not as wrong citations.

Precision (human): the harness exports a CSV for manual labeling (`human_label` = tp/fp) and computes
precision per reason code once the file is filled in (`--labels`).
"""
from __future__ import annotations

import csv
import json
import random
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import PROC_DIR, ROOT, Settings
from .normalize import clean, to_date
from .socrata import fetch_live_row

EVAL_DIR = ROOT / "eval"

# live-field mapping: dataset -> (status field, date field)
LIVE_FIELDS = {
    "filings": ("filing_status", "filing_date"),
    "permits": ("permit_status", "issued_date"),
    "safety_violations": ("violation_status", "violation_issue_date"),
    "legacy_violations": ("violation_category", "issue_date"),
    "ecb_violations": ("ecb_violation_status", "issue_date"),
    "complaints": ("status", "date_entered"),
}


def sample_flags(n: int, seed: int = 7) -> list[dict]:
    by: dict[str, list[dict]] = defaultdict(list)
    with open(PROC_DIR / "flags.jsonl", encoding="utf-8") as fh:
        for line in fh:
            f = json.loads(line)
            by[f["reason_code"]].append(f)
    rnd = random.Random(seed)
    for v in by.values():
        rnd.shuffle(v)
    out, i = [], 0
    while len(out) < n and any(by.values()):
        for code in sorted(by):
            if by[code] and len(out) < n:
                out.append(by[code].pop())
        i += 1
    return out


def _check_evidence(settings: Settings, flag: dict, ev: dict) -> dict:
    try:
        row = fetch_live_row(ev["source_dataset"], ev["source_key"], settings)
    except Exception as e:  # noqa: BLE001
        return {"record_id": ev["record_id"], "exists": None, "error": str(e)[:120]}
    if not row:
        return {"record_id": ev["record_id"], "exists": False}
    s_f, d_f = LIVE_FIELDS[ev["source_dataset"]]
    live_status = clean(row.get(s_f))
    live_date = to_date(row.get(d_f)) or to_date(row.get("approved_date"))
    live_bin = clean(row.get("bin"))
    return {
        "record_id": ev["record_id"], "exists": True,
        "bin_match": live_bin == flag["bin"],
        "status_match": live_status == ev.get("status"),
        "date_match": (live_date == ev.get("date")) if ev.get("date") else None,
        "live_status": live_status,
    }


def run_eval(settings: Settings, n: int = 100, log=print) -> dict:
    EVAL_DIR.mkdir(exist_ok=True)
    flags = sample_flags(n)
    jobs = [(f, e) for f in flags for e in f["evidence"][:3]]
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(lambda je: (je[0], _check_evidence(settings, *je)), jobs))

    ok = [(f, r) for f, r in results if r.get("exists") is not None]
    exists = [r for _, r in ok if r["exists"]]
    summary = {
        "flags_sampled": len(flags), "evidence_checked": len(ok),
        "citation_exists_rate": round(len(exists) / len(ok), 3) if ok else None,
        "bin_match_rate": round(sum(r["bin_match"] for r in exists) / len(exists), 3) if exists else None,
        "status_unchanged_rate": round(sum(r["status_match"] for r in exists) / len(exists), 3) if exists else None,
        "date_match_rate": round(
            sum(1 for r in exists if r["date_match"]) / max(1, sum(1 for r in exists if r["date_match"] is not None)), 3),
        "network_errors": sum(1 for _, r in results if r.get("exists") is None),
    }
    # per reason code: share of flags whose *primary* evidence is still in the flagged state
    per: dict[str, list[bool]] = defaultdict(list)
    first = {}
    for f, r in results:
        first.setdefault(f["flag_id"], (f, r))
    for f, r in first.values():
        if r.get("exists"):
            per[f["reason_code"]].append(bool(r["status_match"]))
    summary["still_in_flagged_state_by_reason"] = {k: f"{sum(v)}/{len(v)}" for k, v in sorted(per.items())}

    tpl = EVAL_DIR / "labels_template.csv"
    with open(tpl, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["flag_id", "reason_code", "severity", "address", "title", "source_url", "human_label(tp/fp)", "notes"])
        for f in flags:
            w.writerow([f["flag_id"], f["reason_code"], f["severity"], f["address"], f["title"],
                        f["evidence"][0]["source_url"], "", ""])
    (EVAL_DIR / "report.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log(json.dumps(summary, indent=2))
    log(f"label template: {tpl}")
    return summary


def precision_from_labels(path: Path) -> dict:
    by: dict[str, list[int]] = defaultdict(list)
    sev: dict[str, list[int]] = defaultdict(list)
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            lab = (row.get("human_label(tp/fp)") or "").strip().lower()
            if lab not in ("tp", "fp"):
                continue
            by[row["reason_code"]].append(lab == "tp")
            sev[row["severity"]].append(lab == "tp")
    f = lambda d: {k: {"n": len(v), "precision": round(sum(v) / len(v), 3)} for k, v in d.items()}  # noqa: E731
    return {"by_reason": f(by), "by_severity": f(sev)}
