"""Mistral function-calling agent over the evidence store.

Mistral decides *which* tool to call; Elasticsearch retrieves; Python rules decide what is flagged.
After the model answers, every `[record_id]` it cites is checked against what the tools actually returned.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .config import Settings
from .extract import extract_record
from .llm import complete, get_client
from .store import BaseStore, slim

SYSTEM_PROMPT = """You are CivicSite, a permit-preflight copilot for small NYC architecture, contractor and expediting teams.
You answer ONLY from tool results about NYC DOB records. Rules:
- Always call tools; never answer from memory. Resolve a building first with resolve_property when given an address.
- For "what should I do / what is blocking" questions call run_preflight_checks and use its flags verbatim.
- Cite evidence as [record_id] exactly as returned by tools (for example [filings:M01234567-I1]). Never invent ids.
- You do not give legal or code-compliance verdicts and do not claim a permit will be approved. Flags are follow-up
  candidates for a human; mention the flag's caveat when it has one.
- Output a short numbered action list: severity, what is blocking, who acts (owner_hint), next step, citation(s).
  Then one line stating the as-of date given below. Be concise.
- Do not describe DOB procedures beyond the tool's recommended_action text (filings are in DOB NOW, not BIS).
- Only pass tool arguments the user actually specified; never invent filters such as a ZIP code.
- Today's as-of date is {as_of}. Data covers Manhattan only (filings from the last 12 months)."""

TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "resolve_property",
        "description": "Resolve a NYC address, 7-digit BIN or 10-digit BBL to candidate buildings (BIN, BBL, address).",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "search_project_records",
        "description": "Hybrid search (BM25 + Mistral semantic) over DOB filings, permits, violations and complaints. "
                       "Use for meaning-based questions like 'illegal conversion' or 'structural cracking'.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "bin": {"type": "string", "description": "Restrict to one building (7-digit BIN)."},
            "record_type": {"type": "string", "enum": ["filing", "permit", "violation", "complaint"]},
            "status_group": {"type": "string", "enum": ["open", "closed", "objections", "on_hold", "in_review",
                                                        "approved", "permitted", "active"]},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_project_timeline",
        "description": "Chronological timeline of all filings, permits, violations and complaints for one building.",
        "parameters": {"type": "object", "properties": {"bin": {"type": "string"}, "limit": {"type": "integer"}},
                       "required": ["bin"]}}},
    {"type": "function", "function": {
        "name": "run_preflight_checks",
        "description": "Run the deterministic rule checks for one building and return ranked, cited flags "
                       "(objections awaiting response, approved-no-permit, permit/violation conflict, expiring permits...).",
        "parameters": {"type": "object", "properties": {"bin": {"type": "string"}}, "required": ["bin"]}}},
    {"type": "function", "function": {
        "name": "list_riskiest_buildings",
        "description": "Portfolio view: buildings with the highest open-flag risk. Pass reason_code or zip ONLY if the user asked for that filter. Each row has top_record_id to cite.",
        "parameters": {"type": "object", "properties": {
            "reason_code": {"type": "string", "enum": [
                "OBJECTIONS_AWAITING_RESPONSE", "ON_HOLD_BLOCKER", "APPROVED_NO_PERMIT", "CHRONOLOGY_CONFLICT",
                "PERMIT_VIOLATION_CONFLICT", "WWP_NO_ACTIVE_PERMIT", "PERMIT_EXPIRING", "PERIODIC_FILING_OVERDUE", "STALE_AGENCY_QUEUE"]},
            "zip": {"type": "string"}, "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "extract_structured_facts",
        "description": "Use Mistral structured output to turn one record's free text into typed facts "
                       "(trades, hazard level, required actions, verbatim quotes).",
        "parameters": {"type": "object", "properties": {"record_id": {"type": "string"}}, "required": ["record_id"]}}},
]


def compact_flag(f: dict) -> dict:
    return {
        "reason_code": f["reason_code"], "severity": f["severity"], "score": f["score"], "title": f["title"],
        "explanation": f["explanation"], "recommended_action": f["recommended_action"], "owner_hint": f["owner_hint"],
        "controlled_by": f["controlled_by"], "confidence": f["confidence"], "caveat": f.get("caveat"),
        "evidence": [{"record_id": e["record_id"], "status": e["status"], "date": e["date"], "url": e["source_url"]}
                     for e in f["evidence"]],
    }


class Toolbox:
    def __init__(self, store: BaseStore, settings: Settings):
        self.store, self.s = store, settings
        self.seen_ids: set[str] = set()

    def _track(self, obj: Any) -> None:
        txt = json.dumps(obj, default=str)
        self.seen_ids.update(re.findall(r'"\w*record_id":\s*"([^"]+)"', txt))

    def call(self, name: str, args: dict) -> Any:
        try:
            out = getattr(self, f"t_{name}")(**args)
        except Exception as e:  # noqa: BLE001
            out = {"error": f"{type(e).__name__}: {e}"}
        self._track(out)
        return out

    def t_resolve_property(self, query: str):
        return {"candidates": [{k: p.get(k) for k in ("bin", "bbl", "address", "zip", "open_flag_count", "risk_score")}
                               for p in self.store.resolve(query, 5)]}

    def t_search_project_records(self, query: str, bin: str | None = None, record_type: str | None = None,
                                 status_group: str | None = None):
        r = self.store.search(query, bin, record_type, status_group, size=8)
        return {"mode": r["mode"], "hits": [{k: h.get(k) for k in (
            "record_id", "record_type", "address", "status", "event_date", "summary", "description", "source_url")}
            for h in r["hits"]]}

    def t_get_project_timeline(self, bin: str, limit: int = 15):
        a = self.store.analysis(bin)
        if not a["found"]:
            return {"error": f"no records for BIN {bin}"}
        return {"address": a["property"]["address"], "events": [
            {k: e[k] for k in ("date", "type", "status", "summary", "record_id", "source_url")}
            for e in a["timeline"][: max(1, min(limit, 40))]]}

    def t_run_preflight_checks(self, bin: str):
        a = self.store.analysis(bin)
        if not a["found"]:
            return {"error": f"no records for BIN {bin}"}
        return {"address": a["property"]["address"], "as_of": a["as_of"], "counts": a["property"]["counts"],
                "open_violations": a["property"]["open_violations"],
                "flags": [compact_flag(f) for f in a["flags"][:12]], "n_flags": len(a["flags"])}

    def t_list_riskiest_buildings(self, reason_code: str | None = None, zip: str | None = None, limit: int = 8):
        r = self.store.portfolio(reason_code, zip, max(1, min(limit, 20)))
        return {"total_buildings_with_flags": r["total_buildings"], "flags_by_reason": r["by_reason"],
                "top": [{k: p.get(k) for k in ("bin", "address", "zip", "risk_score", "open_flag_count", "top_reason",
                                               "top_flag_title", "top_record_id")} for p in r["top"]]}

    def t_extract_structured_facts(self, record_id: str):
        rec = self.store.get_record(record_id)
        if not rec:
            return {"error": f"unknown record_id {record_id}"}
        d = extract_record(self.store, self.s, rec)
        return {"record_id": record_id, "facts": d.get("extracted"), "quotes_verified": d.get("quotes_verified"),
                "model": d.get("extraction_model"), "skipped": d.get("skipped")}


CITE_RE = re.compile(r"\[([a-z_]+:[^\]\s]+)\]")


def citation_check(answer: str, seen: set[str]) -> dict:
    cited = CITE_RE.findall(answer)
    valid = [c for c in cited if c in seen]
    return {"cited": len(cited), "valid": len(valid), "invalid_ids": sorted(set(cited) - seen),
            "citation_accuracy": round(len(valid) / len(cited), 3) if cited else None}


def ask(store: BaseStore, settings: Settings, question: str, bin_hint: str | None = None, max_steps: int = 6) -> dict:
    client = get_client(settings)
    tb = Toolbox(store, settings)
    user = question if not bin_hint else f"{question}\n(Context: the user is looking at BIN {bin_hint}.)"
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT.replace("{as_of}", settings.as_of.isoformat())}, {"role": "user", "content": user}]
    trace: list[dict] = []
    model_used = settings.chat_model
    answer = ""
    for _ in range(max_steps):
        model_used, resp = complete(client, settings.chat_model, messages=messages, tools=TOOLS,
                                    tool_choice="auto", temperature=0.1)
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        if not calls:
            answer = msg.content if isinstance(msg.content, str) else "".join(
                getattr(c, "text", "") for c in (msg.content or []))
            break
        messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
            {"id": c.id, "type": "function", "function": {
                "name": c.function.name,
                "arguments": c.function.arguments if isinstance(c.function.arguments, str)
                else json.dumps(c.function.arguments)}} for c in calls]})
        for c in calls:
            args = c.function.arguments
            args = json.loads(args) if isinstance(args, str) else dict(args or {})
            out = tb.call(c.function.name, args)
            trace.append({"tool": c.function.name, "args": args,
                          "summary": (f"error: {out['error']}" if isinstance(out, dict) and "error" in out
                                      else _summ(out))})
            messages.append({"role": "tool", "name": c.function.name, "tool_call_id": c.id,
                             "content": json.dumps(out, default=str)[:24000]})
    else:
        answer = answer or "(stopped: too many tool steps)"
    return {"answer": answer, "model": model_used, "tool_trace": trace,
            "citations": citation_check(answer, tb.seen_ids), "backend": store.backend}


def _summ(out: Any) -> str:
    if isinstance(out, dict):
        for k in ("flags", "hits", "events", "candidates", "top"):
            if k in out and isinstance(out[k], list):
                return f"{len(out[k])} {k}"
        if "facts" in out:
            return "structured facts extracted"
    return "ok"
