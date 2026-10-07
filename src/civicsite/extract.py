"""Mistral structured extraction: messy DOB free text -> typed, *verifiable* facts.

The LLM may only extract; every `key_quote` it returns must appear verbatim in the source text.
We measure that (`quotes_verified`) and store it next to the extraction, so a downstream user can see
how much of the model's output is literally grounded in the record.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from .config import Settings
from .llm import get_client, parse

Trade = Literal["general_construction", "plumbing", "electrical", "structural", "mechanical_hvac",
                "sprinkler_fire", "elevator", "facade_exterior", "demolition", "signage_shed_fence", "other"]


class ExtractedFacts(BaseModel):
    work_summary: str = Field(description="One sentence, max 30 words, plain English summary of the work or violation.")
    trades: list[Trade] = Field(default_factory=list, description="Trades involved; empty if not stated.")
    building_use: str | None = Field(default=None, description="Occupancy/use if stated (e.g. 'residential', 'office'), else null.")
    action_required_by: Literal["applicant", "owner", "contractor", "dob", "unknown"] = "unknown"
    hazard_level: Literal["low", "medium", "high", "unknown"] = Field(
        default="unknown", description="Only 'high' if the text mentions imminent danger, stop work, vacate, collapse, fire or structural hazard.")
    required_actions: list[str] = Field(default_factory=list, description="Concrete corrective actions explicitly stated; empty if none.")
    key_quotes: list[str] = Field(default_factory=list, description="1-3 short quotes COPIED VERBATIM from the text that support the summary.")


SYSTEM = (
    "You extract structured facts from NYC Department of Buildings records (job descriptions and violation text). "
    "Use ONLY what is explicitly written. Never infer legal conclusions. If a field is not stated, use null/empty/unknown. "
    "key_quotes must be exact substrings of the input."
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_quotes(quotes: list[str], source: str) -> float:
    if not quotes:
        return 0.0
    src = _norm(source)
    return sum(1 for q in quotes if _norm(q) and _norm(q) in src) / len(quotes)


def evidence_id(record_id: str, text: str, model: str) -> str:
    return hashlib.sha1(f"{record_id}|{model}|{text}".encode()).hexdigest()[:20]


def extract_record(store, settings: Settings, record: dict, force: bool = False) -> dict:
    text = record.get("description") or record.get("summary") or ""
    if len(text) < 15:
        return {"record_id": record["record_id"], "skipped": "no descriptive text"}
    eid = evidence_id(record["record_id"], text, settings.extract_model)
    if not force and (cached := store.get_evidence(eid)):
        return cached | {"cached": True}

    client = get_client(settings)
    used, resp = parse(
        client, settings.extract_model, ExtractedFacts, temperature=0,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": f"Record type: {record['record_type']} ({record['source_dataset']})\n"
                                              f"Status: {record.get('status')}\nText:\n{text}"}],
    )
    facts: ExtractedFacts = resp.choices[0].message.parsed
    qv = verify_quotes(facts.key_quotes, text)
    doc = {
        "evidence_id": eid, "record_id": record["record_id"], "bin": record["bin"], "text_hash": eid,
        "extraction_model": used, "extracted": facts.model_dump(), "quotes_verified": qv,
        "confidence": round(0.5 + 0.5 * qv, 2) if facts.key_quotes else 0.4,
        "created_at": datetime.now(timezone.utc).isoformat(), "source_text": text[:2000],
        "source_url": record["source_url"],
    }
    store.put_evidence({k: v for k, v in doc.items() if k != "source_url"})
    return doc
