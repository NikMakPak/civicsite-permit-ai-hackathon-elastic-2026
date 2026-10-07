"""Retrieval layer. Two interchangeable backends:

* ElasticStore - the real thing: BM25 + Mistral `semantic_text` fused with an RRF retriever, ES filters,
  aggregations. Used for the demo and the product.
* LocalStore  - reads data/processed/*.jsonl; lexical only. Used for tests, offline dev and as a
  safety net if the venue wifi dies mid-demo.

Deterministic checks always run in Python on the retrieved records (see checks.py).
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from elasticsearch import Elasticsearch
from elasticsearch.exceptions import ApiError

from .checks import run_checks, timeline
from .config import PROC_DIR, Settings
from .es_setup import get_es, read_jsonl

LIST_FIELDS = [
    "record_id", "record_type", "source_dataset", "bin", "bbl", "address", "status", "status_group",
    "event_date", "summary", "description", "source_url", "work_type", "severity", "job_filing_number",
    "permit_number", "violation_number",
]

ABBR = {"AVE": "AVENUE", "AV": "AVENUE", "ST": "STREET", "STR": "STREET", "BLVD": "BOULEVARD", "RD": "ROAD",
        "PL": "PLACE", "DR": "DRIVE", "PKWY": "PARKWAY", "LN": "LANE", "HWY": "HIGHWAY", "CT": "COURT",
        "W": "WEST", "E": "EAST", "N": "NORTH", "S": "SOUTH"}
NOISE = {"NEW", "YORK", "NY", "NYC", "MANHATTAN", "USA"}


def normalize_address_query(q: str) -> str:
    q = re.sub(r"[^\w\s-]", " ", q.upper())
    toks = [t for t in q.split() if t not in NOISE and not re.fullmatch(r"\d{5}", t)]
    out = []
    for i, t in enumerate(toks):
        t = re.sub(r"^(\d+)(ST|ND|RD|TH)$", r"\1", t)  # 117TH -> 117
        if t in ABBR and i > 0:
            if t in ("W", "E", "N", "S") and i > 2:
                pass
            else:
                t = ABBR[t]
        out.append(t)
    return " ".join(out)


def slim(rec: dict, desc_len: int = 400) -> dict:
    d = {k: rec.get(k) for k in LIST_FIELDS if rec.get(k) is not None}
    if d.get("description"):
        d["description"] = d["description"][:desc_len]
    return d


class BaseStore:
    backend = "base"

    def __init__(self, settings: Settings):
        self.s = settings

    # --- to implement
    def resolve(self, q: str, limit: int = 5) -> list[dict]: ...
    def records(self, bin_: str) -> list[dict]: ...
    def search(self, q: str, bin_: str | None = None, record_type: str | None = None,
               status_group: str | None = None, size: int = 8) -> dict: ...
    def portfolio(self, reason: str | None = None, zip_: str | None = None, limit: int = 10) -> dict: ...
    def get_record(self, record_id: str) -> dict | None: ...
    def stats(self) -> dict: ...

    # --- shared
    def analysis(self, bin_: str) -> dict:
        recs = self.records(bin_)
        if not recs:
            return {"found": False, "bin": bin_}
        flags = run_checks(recs, self.s.as_of)
        base = next((r for r in recs if r.get("address")), recs[0])
        counts = Counter(r["record_type"] for r in recs)
        open_v = sum(1 for r in recs if r["record_type"] == "violation" and r["status_group"] == "open"
                     and r.get("data_quality") != "ecb_mirror"
                     and r.get("violation_class") != "periodic_compliance")
        return {
            "found": True, "bin": bin_, "as_of": self.s.as_of.isoformat(),
            "property": {"bin": bin_, "bbl": base.get("bbl"), "address": base.get("address"), "zip": base.get("zip"),
                         "location": base.get("location"), "counts": dict(counts), "open_violations": open_v},
            "flags": flags, "timeline": timeline(recs, 120),
        }


# =============================================================================== Elasticsearch


class ElasticStore(BaseStore):
    backend = "elastic"

    def __init__(self, settings: Settings, es: Elasticsearch | None = None):
        super().__init__(settings)
        self.es = es or get_es(settings)
        self._hybrid_ok = True

    def resolve(self, q: str, limit: int = 5) -> list[dict]:
        q = q.strip()
        if re.fullmatch(r"\d{7}", q):
            query = {"term": {"bin": q}}
        elif re.fullmatch(r"\d{10}", q):
            query = {"term": {"bbl": q}}
        else:
            nq = normalize_address_query(q)
            query = {"bool": {"must": [{"match": {"address": {"query": nq, "operator": "and", "fuzziness": "AUTO"}}}],
                              "should": [{"match_phrase": {"address": {"query": nq, "boost": 3}}}]}}
        r = self.es.search(index=self.s.idx_projects, query=query, size=limit,
                           sort=["_score", {"risk_score": "desc"}])
        return [h["_source"] | {"_score": h["_score"]} for h in r["hits"]["hits"]]

    def records(self, bin_: str) -> list[dict]:
        r = self.es.search(index=self.s.idx_records, query={"term": {"bin": bin_}}, size=3000,
                           sort=[{"event_date": {"order": "desc", "unmapped_type": "date"}}],
                           source_excludes=["description_semantic"])
        return [h["_source"] for h in r["hits"]["hits"]]

    def get_record(self, record_id: str) -> dict | None:
        r = self.es.search(index=self.s.idx_records, query={"term": {"record_id": record_id}}, size=1,
                           source_excludes=["description_semantic"])
        hits = r["hits"]["hits"]
        return hits[0]["_source"] if hits else None

    def _filters(self, bin_, record_type, status_group) -> list[dict]:
        f = []
        if bin_:
            f.append({"term": {"bin": bin_}})
        if record_type:
            f.append({"term": {"record_type": record_type}})
        if status_group:
            f.append({"term": {"status_group": status_group}})
        return f

    def search(self, q, bin_=None, record_type=None, status_group=None, size=8) -> dict:
        filters = self._filters(bin_, record_type, status_group)
        lexical = {"bool": {"must": [{"multi_match": {
            "query": q, "fields": ["description^2", "summary", "address", "work_type", "applicant", "owner"]}}],
            "filter": filters}}
        mode = "lexical"
        hits = None
        if self._hybrid_ok:
            body = {"retriever": {"rrf": {"retrievers": [
                {"standard": {"query": lexical}},
                {"standard": {"query": {"bool": {"must": [{"semantic": {"field": "description_semantic", "query": q}}],
                                                 "filter": filters}}}},
            ], "rank_window_size": 50, "rank_constant": 20}}}
            try:
                r = self.es.search(index=self.s.idx_records, size=size, source_excludes=["description_semantic"], **body)
                hits, mode = r["hits"]["hits"], "hybrid_rrf(bm25+mistral_semantic)"
            except ApiError as e:
                self._hybrid_ok = False
                self._hybrid_err = str(getattr(e, "message", e))[:200]
        if hits is None:
            r = self.es.search(index=self.s.idx_records, query=lexical, size=size, source_excludes=["description_semantic"])
            hits = r["hits"]["hits"]
        return {"mode": mode, "hits": [slim(h["_source"]) | {"_score": h.get("_score")} for h in hits]}

    def portfolio(self, reason=None, zip_=None, limit=10) -> dict:
        filters: list[dict] = [{"range": {"open_flag_count": {"gt": 0}}}]
        if reason:
            filters.append({"term": {"flag_reasons": reason}})
        if zip_:
            filters.append({"term": {"zip": zip_}})
        r = self.es.search(index=self.s.idx_projects, query={"bool": {"filter": filters}}, size=limit,
                           sort=[{"risk_score": "desc"}], track_total_hits=True)
        fl_filters = []
        if reason:
            fl_filters.append({"term": {"reason_code": reason}})
        a = self.es.search(index=self.s.idx_flags, size=0, query={"bool": {"filter": fl_filters}}, aggs={
            "by_reason": {"terms": {"field": "reason_code", "size": 20}},
            "by_severity": {"terms": {"field": "severity", "size": 5}},
        })
        return {
            "total_buildings": r["hits"]["total"]["value"],
            "top": [h["_source"] for h in r["hits"]["hits"]],
            "by_reason": {b["key"]: b["doc_count"] for b in a["aggregations"]["by_reason"]["buckets"]},
            "by_severity": {b["key"]: b["doc_count"] for b in a["aggregations"]["by_severity"]["buckets"]},
        }

    def stats(self) -> dict:
        out = {}
        for name in (self.s.idx_records, self.s.idx_flags, self.s.idx_projects, self.s.idx_evidence):
            try:
                out[name] = self.es.count(index=name)["count"]
            except ApiError:
                out[name] = None
        return out

    # evidence cache (Mistral extractions)
    def get_evidence(self, evidence_id: str) -> dict | None:
        try:
            return self.es.get(index=self.s.idx_evidence, id=evidence_id)["_source"]
        except ApiError:
            return None

    def put_evidence(self, doc: dict) -> None:
        self.es.index(index=self.s.idx_evidence, id=doc["evidence_id"], document=doc, refresh="wait_for")


# =============================================================================== Local (offline)

_tok = re.compile(r"[a-z0-9]+")


class LocalStore(BaseStore):
    backend = "local"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._by_bin: dict[str, list[dict]] = defaultdict(list)
        self._by_id: dict[str, dict] = {}
        for r in read_jsonl(PROC_DIR / "records.jsonl"):
            self._by_bin[r["bin"]].append(r)
            self._by_id[r["record_id"]] = r
        self.projects = list(read_jsonl(PROC_DIR / "projects.jsonl"))
        self.flags = list(read_jsonl(PROC_DIR / "flags.jsonl"))
        self._evidence: dict[str, dict] = {}

    def resolve(self, q: str, limit: int = 5) -> list[dict]:
        q = q.strip()
        if re.fullmatch(r"\d{7}", q):
            return [p for p in self.projects if p["bin"] == q][:limit]
        if re.fullmatch(r"\d{10}", q):
            return [p for p in self.projects if p.get("bbl") == q][:limit]
        want = set(_tok.findall(normalize_address_query(q).lower()))
        scored = []
        for p in self.projects:
            have = set(_tok.findall((p.get("address") or "").lower()))
            if want and want <= have:
                scored.append((len(have), p))
        scored.sort(key=lambda x: (x[0], -x[1]["risk_score"]))
        return [p for _, p in scored[:limit]]

    def records(self, bin_: str) -> list[dict]:
        return sorted(self._by_bin.get(bin_, []), key=lambda r: r.get("event_date") or "", reverse=True)

    def get_record(self, record_id: str) -> dict | None:
        return self._by_id.get(record_id)

    def search(self, q, bin_=None, record_type=None, status_group=None, size=8) -> dict:
        want = [t for t in _tok.findall(q.lower()) if len(t) > 2]
        pool = self._by_bin.get(bin_, []) if bin_ else self._by_id.values()
        scored = []
        for r in pool:
            if record_type and r["record_type"] != record_type:
                continue
            if status_group and r["status_group"] != status_group:
                continue
            text = f"{r.get('description') or ''} {r.get('summary') or ''} {r.get('work_type') or ''}".lower()
            sc = sum(text.count(t) for t in want)
            if sc:
                scored.append((sc, r))
        scored.sort(key=lambda x: (-x[0], x[1].get("event_date") or ""), reverse=False)
        return {"mode": "lexical(local)", "hits": [slim(r) | {"_score": sc} for sc, r in scored[:size]]}

    def portfolio(self, reason=None, zip_=None, limit=10) -> dict:
        ps = [p for p in self.projects if p["open_flag_count"] > 0
              and (not reason or reason in p["flag_reasons"]) and (not zip_ or p.get("zip") == zip_)]
        ps.sort(key=lambda p: -p["risk_score"])
        fl = [f for f in self.flags if not reason or f["reason_code"] == reason]
        return {
            "total_buildings": len(ps), "top": ps[:limit],
            "by_reason": dict(Counter(f["reason_code"] for f in fl)),
            "by_severity": dict(Counter(f["severity"] for f in fl)),
        }

    def stats(self) -> dict:
        return {"records": len(self._by_id), "flags": len(self.flags), "projects": len(self.projects)}

    def get_evidence(self, evidence_id: str) -> dict | None:
        return self._evidence.get(evidence_id)

    def put_evidence(self, doc: dict) -> None:
        self._evidence[doc["evidence_id"]] = doc


def get_store(settings: Settings, backend: str = "auto") -> BaseStore:
    backend = (backend or "auto").lower()
    if backend == "local":
        return LocalStore(settings)
    if backend in ("elastic", "auto"):
        try:
            es = get_es(settings)
            if settings.es_api_key or "localhost" in settings.es_url:
                es.info()
                if es.indices.exists(index=settings.idx_projects).body:
                    return ElasticStore(settings, es)
                if backend == "elastic":
                    raise RuntimeError(f"index {settings.idx_projects} not found - run `civicsite setup && civicsite index`")
        except Exception:
            if backend == "elastic":
                raise
        return LocalStore(settings)
    raise ValueError(backend)
