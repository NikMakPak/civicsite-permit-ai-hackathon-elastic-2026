"""Elasticsearch (Serverless-compatible) client, Mistral inference endpoints, index mappings, bulk indexing."""
from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator

from elasticsearch import Elasticsearch, NotFoundError, helpers
from elasticsearch.exceptions import ApiError

from .config import PROC_DIR, Settings


def get_es(settings: Settings) -> Elasticsearch:
    kw: dict = {"request_timeout": 120, "retry_on_timeout": True, "max_retries": 3}
    if settings.es_api_key:
        kw["api_key"] = settings.es_api_key
    return Elasticsearch(settings.es_url, **kw)


# ----------------------------------------------------------------------------- inference endpoints


def _call(es: Elasticsearch, method: str, path: str, body: dict | None = None):
    hdrs = {"content-type": "application/json", "accept": "application/json"}
    return es.perform_request(method, path, headers=hdrs, body=body)


def ensure_inference_endpoints(es: Elasticsearch, s: Settings, log=print) -> dict[str, bool]:
    """Register Mistral models *inside* Elasticsearch: an embedding endpoint (for semantic_text)
    and a chat_completion endpoint (usable from Agent Builder / _inference). Idempotent."""
    if not s.mistral_api_key:
        log("MISTRAL_API_KEY missing - skipping inference endpoints")
        return {"embeddings": False, "chat": False}
    status = {}
    specs = [
        ("embeddings", "text_embedding", s.embed_inference_id, s.embed_model),
        ("chat", "chat_completion", s.chat_inference_id, s.chat_model),
    ]
    for label, task, inf_id, model in specs:
        path = f"/_inference/{task}/{inf_id}"
        try:
            _call(es, "GET", path)
            log(f"inference endpoint exists: {inf_id}")
            status[label] = True
            continue
        except NotFoundError:
            pass
        except ApiError as e:  # some stacks answer 400 for unknown ids
            if e.status_code not in (400, 404):
                raise
        body = {"service": "mistral", "service_settings": {"api_key": s.mistral_api_key, "model": model}}
        try:
            _call(es, "PUT", path, body)
            log(f"created inference endpoint {inf_id} ({task}, {model})")
            status[label] = True
        except ApiError as e:
            log(f"!! could not create {inf_id}: {e.status_code} {getattr(e, 'message', e)}")
            status[label] = False
    return status


# ----------------------------------------------------------------------------- mappings


def _text_kw(analyzer: str | None = None) -> dict:
    t: dict = {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}}
    if analyzer:
        t["analyzer"] = analyzer
    return t


def records_mapping(s: Settings, semantic: bool = True) -> dict:
    kw = {"type": "keyword"}
    dt = {"type": "date", "format": "yyyy-MM-dd||strict_date_optional_time"}
    props = {
        "record_id": kw, "source_dataset": kw, "source_dataset_id": kw, "source_key": kw,
        "source_url": {"type": "keyword", "ignore_above": 512},
        "record_type": kw, "bin": kw, "bbl": kw, "borough": kw, "zip": kw,
        "address": _text_kw(), "job_filing_number": kw, "permit_number": kw, "violation_number": kw,
        "status": kw, "status_group": kw, "status_date": dt, "event_date": dt, "filing_date": dt,
        "approved_date": dt, "first_permit_date": dt, "signoff_date": dt, "issued_date": dt, "expired_date": dt,
        "work_type": kw, "severity": kw, "is_wwp": {"type": "boolean"}, "data_quality": kw,
        "violation_class": kw, "linked_ecb": kw, "linked_dob_violation": kw,
        "summary": {"type": "text"},
        "description": {"type": "text", "analyzer": "english"},
        "cost": {"type": "double"},
        "applicant": _text_kw(), "owner": _text_kw(), "filing_rep": _text_kw(),
        "location": {"type": "geo_point"},
        "ingested_at": dt,
    }
    if semantic:
        props["description_semantic"] = {"type": "semantic_text", "inference_id": s.embed_inference_id}
    return {"properties": props}


def flags_mapping() -> dict:
    kw = {"type": "keyword"}
    dt = {"type": "date", "format": "yyyy-MM-dd||strict_date_optional_time"}
    return {"properties": {
        "flag_id": kw, "bin": kw, "bbl": kw, "address": _text_kw(), "reason_code": kw,
        "title": {"type": "text"}, "severity": kw, "score": {"type": "float"}, "controlled_by": kw,
        "days": {"type": "integer"}, "explanation": {"type": "text"}, "recommended_action": {"type": "text"},
        "owner_hint": kw, "confidence": {"type": "float"}, "join_basis": kw, "caveat": {"type": "text"},
        "as_of": dt, "computed_at": dt,
        "evidence": {"type": "object", "properties": {
            "record_id": kw, "source_dataset": kw, "source_dataset_id": kw, "source_key": kw,
            "source_url": {"type": "keyword", "ignore_above": 512}, "record_type": kw,
            "date": dt, "status": kw, "summary": {"type": "text"}, "note": {"type": "text"},
        }},
    }}


def projects_mapping() -> dict:
    kw = {"type": "keyword"}
    return {"properties": {
        "bin": kw, "bbl": kw, "address": _text_kw(), "zip": kw, "location": {"type": "geo_point"},
        "n_filings": {"type": "integer"}, "n_permits": {"type": "integer"}, "n_violations": {"type": "integer"},
        "n_complaints": {"type": "integer"}, "n_open_violations": {"type": "integer"},
        "open_flag_count": {"type": "integer"}, "flag_reasons": kw, "top_reason": kw, "top_record_id": kw,
        "top_flag_title": {"type": "text"}, "risk_score": {"type": "float"},
        "last_activity": {"type": "date", "format": "yyyy-MM-dd||strict_date_optional_time"},
    }}


def evidence_mapping() -> dict:
    kw = {"type": "keyword"}
    return {"properties": {
        "evidence_id": kw, "record_id": kw, "bin": kw, "text_hash": kw, "extraction_model": kw,
        "confidence": {"type": "float"}, "quotes_verified": {"type": "float"},
        "created_at": {"type": "date"}, "source_text": {"type": "text"},
        "extracted": {"type": "object", "properties": {
            "work_summary": {"type": "text"}, "trades": kw, "building_use": kw, "action_required_by": kw,
            "hazard_level": kw, "required_actions": {"type": "text"}, "key_quotes": {"type": "text"},
        }},
    }}


def create_indices(es: Elasticsearch, s: Settings, recreate: bool = False, semantic: bool = True, log=print) -> None:
    plan = {
        s.idx_records: records_mapping(s, semantic),
        s.idx_flags: flags_mapping(),
        s.idx_projects: projects_mapping(),
        s.idx_evidence: evidence_mapping(),
    }
    for name, mapping in plan.items():
        exists = es.indices.exists(index=name).body
        if exists and recreate:
            es.indices.delete(index=name)
            exists = False
        if not exists:
            es.indices.create(index=name, mappings=mapping)
            log(f"created index {name}")
        else:
            log(f"index {name} exists (kept)")


# ----------------------------------------------------------------------------- bulk indexing


def read_jsonl(path) -> Iterator[dict]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def _bulk(es: Elasticsearch, index: str, docs: Iterable[dict], id_field: str, chunk: int, log=print) -> tuple[int, int]:
    ok = bad = 0
    actions = ({"_index": index, "_id": d[id_field], "_source": d} for d in docs)
    for success, info in helpers.streaming_bulk(
        es, actions, chunk_size=chunk, max_retries=6, initial_backoff=2, max_backoff=60,
        raise_on_error=False, raise_on_exception=False, request_timeout=180,
    ):
        if success:
            ok += 1
        else:
            bad += 1
            if bad <= 5:
                log(f"  bulk error: {json.dumps(info)[:400]}")
        if (ok + bad) % 10000 == 0:
            log(f"  {index}: {ok + bad} docs...")
    return ok, bad


def index_all(es: Elasticsearch, s: Settings, log=print) -> dict[str, tuple[int, int]]:
    out = {}
    t = time.time()
    recs = list(read_jsonl(PROC_DIR / "records.jsonl"))
    plain = (r for r in recs if "description_semantic" not in r)
    sem = [r for r in recs if "description_semantic" in r]
    log(f"indexing {len(recs)} records ({len(sem)} with semantic_text)")
    ok1, bad1 = _bulk(es, s.idx_records, plain, "record_id", 1000, log)
    ok2, bad2 = _bulk(es, s.idx_records, sem, "record_id", 64, log)  # embedding calls happen at ingest
    out["records"] = (ok1 + ok2, bad1 + bad2)
    out["flags"] = _bulk(es, s.idx_flags, read_jsonl(PROC_DIR / "flags.jsonl"), "flag_id", 1000, log)
    out["projects"] = _bulk(es, s.idx_projects, read_jsonl(PROC_DIR / "projects.jsonl"), "bin", 1000, log)
    es.indices.refresh(index=",".join([s.idx_records, s.idx_flags, s.idx_projects]))
    log(f"done in {time.time() - t:.0f}s: {out}")
    return out
