from datetime import date

from civicsite.checks import run_checks
from civicsite.normalize import filing_group, make_bbl, to_date, norm_filing, norm_permit, norm_legacy
from civicsite.store import normalize_address_query

TODAY = date(2026, 10, 7)


def rec(**kw):
    base = {
        "record_id": "x:1", "source_dataset": "filings", "source_dataset_id": "w9ak-ipjd", "source_key": "1",
        "source_url": "https://example/1", "record_type": "filing", "bin": "1000001", "bbl": "1000010001",
        "address": "1 TEST STREET", "status": "Approved", "status_group": "approved", "event_date": "2026-01-01",
        "status_date": "2026-01-01", "is_wwp": False, "data_quality": None,
    }
    return base | kw


def test_dates_and_bbl():
    assert to_date("20260901") == "2026-09-01"
    assert to_date("09/06/2025") == "2025-09-06"
    assert to_date("Y9990120") is None  # junk seen in DOB Violations
    assert to_date("2026-10-07T00:00:00.000") == "2026-10-07"
    assert make_bbl("1", "01923", "00016") == "1019230016"
    assert make_bbl("Manhattan", "1395", "26") == "1013950026"
    assert make_bbl(None, None, None, "1013950026") == "1013950026"


def test_filing_groups():
    assert filing_group("Objections") == "objections"
    assert filing_group("On Hold - Pending Supersede of Applicant of Record") == "on_hold"
    assert filing_group("Permit Entire") == "permitted"
    assert filing_group("???") == "other"


def test_objections_flag_and_threshold():
    f = rec(job_filing_number="M1-I1", status="Objections", status_group="objections", status_date="2026-08-01")
    flags = run_checks([f], TODAY)
    assert [x["reason_code"] for x in flags] == ["OBJECTIONS_AWAITING_RESPONSE"]
    assert flags[0]["severity"] == "high" and flags[0]["controlled_by"] == "applicant"
    assert flags[0]["evidence"][0]["source_url"] == "https://example/1"
    fresh = rec(job_filing_number="M1-I1", status="Objections", status_group="objections", status_date="2026-10-01")
    assert run_checks([fresh], TODAY) == []


def test_approved_no_permit_only_for_initial_and_subsequent_filings():
    base = dict(status="Approved", status_group="approved", approved_date="2026-06-01", status_date="2026-06-01",
                work_type="Alteration")
    assert run_checks([rec(job_filing_number="M1-I1", **base)], TODAY)[0]["reason_code"] == "APPROVED_NO_PERMIT"
    assert run_checks([rec(job_filing_number="M1-P3", **base)], TODAY) == []  # PAA never carries a permit
    assert run_checks([rec(job_filing_number="M1-I1", **(base | {"work_type": "No Work"}))], TODAY) == []
    permit = rec(record_id="permits:9", record_type="permit", source_dataset="permits", job_filing_number="M1-I1",
                 status="Permit Issued", status_group="active", issued_date="2026-06-10", expired_date="2027-06-10",
                 permit_number="M1-I1-GC")
    assert run_checks([rec(job_filing_number="M1-I1", **base), permit], TODAY) == []


def test_permit_violation_conflict_and_wwp():
    permit = rec(record_id="permits:9", record_type="permit", source_dataset="permits", job_filing_number="M1-I1",
                 status="Permit Issued", status_group="active", issued_date="2026-06-10", expired_date="2027-06-10",
                 permit_number="M1-I1-GC")
    viol = rec(record_id="ecb_violations:5", record_type="violation", source_dataset="ecb_violations",
               source_key="5", status="ACTIVE", status_group="open", event_date="2026-09-01", severity="CLASS - 1",
               violation_number="5", description="stop work")
    codes = [f["reason_code"] for f in run_checks([permit, viol], TODAY)]
    assert "PERMIT_VIOLATION_CONFLICT" in codes
    wwp = viol | {"is_wwp": True}
    codes2 = [f["reason_code"] for f in run_checks([wwp], TODAY)]
    assert codes2 == ["WWP_NO_ACTIVE_PERMIT"]


def test_ecb_mirror_not_double_counted():
    permit = rec(record_id="permits:9", record_type="permit", source_dataset="permits", job_filing_number="M1-I1",
                 status="Permit Issued", status_group="active", issued_date="2026-06-10", expired_date="2027-06-10")
    mirror = rec(record_id="legacy_violations:7", record_type="violation", status="VP-VIOLATION UNSERVED ECB-ACTIVE",
                 status_group="open", event_date="2026-09-01", data_quality="ecb_mirror")
    assert run_checks([permit, mirror], TODAY) == []


def test_permit_expiring_uses_latest_renewal_row():
    old = rec(record_id="permits:1", record_type="permit", source_dataset="permits", job_filing_number="M1-I1",
              permit_number="M1-I1-GC", status="Permit Issued", status_group="active", issued_date="2025-10-10",
              expired_date="2026-10-15")
    renewed = old | {"record_id": "permits:2", "issued_date": "2026-10-01", "expired_date": "2027-10-01"}
    assert run_checks([old], TODAY)[0]["reason_code"] == "PERMIT_EXPIRING"
    assert run_checks([old, renewed], TODAY) == []


def test_placeholder_permit_rows_are_ignored():
    r = norm_permit({"job_filing_number": "Permit is no", "work_permit": "Permit is not yet issued",
                     "tracking_number": "1", "bin": "1000001", "permit_status": "Signed-off"}, "now")
    assert r["data_quality"] == "placeholder_permit_row" and r["job_filing_number"] is None


def test_norm_filing_and_legacy_mirror():
    f = norm_filing({"job_filing_number": "M1-I1", "bin": "1000001", "borough": "Manhattan", "block": "1", "lot": "2",
                     "house_no": "5", "street_name": "Main Street", "filing_status": "Objections",
                     "filing_date": "2026-01-02T00:00:00.000", "current_status_date": "2026-01-03T00:00:00.000"}, "now")
    assert f["status_group"] == "objections" and f["bbl"] == "1000010002" and f["address"] == "5 MAIN STREET"
    v = norm_legacy({"number": "V1", "bin": "1000001", "boro": "1", "violation_category": "VP-VIOLATION UNSERVED ECB-ACTIVE",
                     "ecb_number": "123", "issue_date": "20260901"}, "now")
    assert v["data_quality"] == "ecb_mirror" and v["status_group"] == "open"


def test_address_normalization():
    assert normalize_address_query("439 E 77th St, New York, NY 10021") == "439 EAST 77 STREET"
    assert normalize_address_query("1 wall st") == "1 WALL STREET"


def test_citation_check_handles_comma_lists():
    from civicsite.agent import citation_check
    seen = {"filings:A-I1", "permits:1", "ecb_violations:9Z"}
    r = citation_check("x [filings:A-I1] y [permits:1, ecb_violations:9Z] z [permits:404]", seen)
    assert (r["cited"], r["valid"], r["invalid_ids"]) == (4, 3, ["permits:404"])
