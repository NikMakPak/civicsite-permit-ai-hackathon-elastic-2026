"""Register CivicSite as an Elastic Agent Builder agent (bonus criterion) + expose it over MCP.

Creates ES|QL tools (precise, parameterised, repeatable) and one index_search tool, then an agent that
uses them. Everything is idempotent: existing tools/agents are replaced.
"""
from __future__ import annotations

import httpx

from .config import Settings

AGENT_ID = "civicsite-copilot"

INSTRUCTIONS = """You are CivicSite, a permit-preflight copilot for NYC DOB records (Manhattan, last 12 months).
Always use tools. Flags come from deterministic rules; never invent a violation or status.
For a building: call civicsite.property_flags with its 7-digit BIN, then civicsite.property_timeline for context.
Cite record_id and source_url for every claim. Do not give legal or code-compliance verdicts.
Present results as a short action list: severity, blocker, who acts, next step, citation."""


def _tools(idx: dict[str, str], param_str: str = "string") -> list[dict]:
    f, r, p = idx["flags"], idx["records"], idx["projects"]
    return [
        {"id": "civicsite.property_flags", "type": "esql",
         "description": "Ranked preflight flags (blockers, conflicts, expiring permits) with cited evidence for one building, by 7-digit BIN.",
         "tags": ["civicsite", "permits"],
         "configuration": {
             "query": f"FROM {f} | WHERE bin == ?bin | SORT score DESC | KEEP reason_code, severity, score, title, "
                      "recommended_action, owner_hint, controlled_by, confidence, caveat, evidence.record_id, "
                      "evidence.source_url, evidence.status, evidence.date | LIMIT 25",
             "params": {"bin": {"type": param_str, "description": "7-digit NYC Building Identification Number"}}}},
        {"id": "civicsite.property_timeline", "type": "esql",
         "description": "Chronological timeline of DOB filings, permits, violations and complaints for one building (BIN).",
         "tags": ["civicsite", "timeline"],
         "configuration": {
             "query": f"FROM {r} | WHERE bin == ?bin | SORT event_date DESC | KEEP event_date, record_type, "
                      "source_dataset, status, summary, record_id, source_url | LIMIT ?limit",
             "params": {"bin": {"type": param_str, "description": "7-digit NYC BIN"},
                        "limit": {"type": "integer", "description": "Max events", "optional": True, "defaultValue": 25}}}},
        {"id": "civicsite.riskiest_buildings", "type": "esql",
         "description": "Portfolio ranking: buildings with the highest open-flag risk score.",
         "tags": ["civicsite", "portfolio"],
         "configuration": {
             "query": f"FROM {p} | WHERE open_flag_count > 0 | SORT risk_score DESC | KEEP bin, address, zip, "
                      "risk_score, open_flag_count, top_reason, top_flag_title | LIMIT ?limit",
             "params": {"limit": {"type": "integer", "description": "Rows", "optional": True, "defaultValue": 10}}}},
        {"id": "civicsite.flags_for_reason", "type": "esql",
         "description": "Top flags for one reason code, e.g. OBJECTIONS_AWAITING_RESPONSE, PERMIT_VIOLATION_CONFLICT, "
                        "WWP_NO_ACTIVE_PERMIT, APPROVED_NO_PERMIT, PERMIT_EXPIRING, ON_HOLD_BLOCKER.",
         "tags": ["civicsite", "portfolio"],
         "configuration": {
             "query": f"FROM {f} | WHERE reason_code == ?reason | SORT score DESC | KEEP bin, address, severity, "
                      "score, title, days | LIMIT ?limit",
             "params": {"reason": {"type": param_str, "description": "Reason code, upper snake case"},
                        "limit": {"type": "integer", "description": "Rows", "optional": True, "defaultValue": 15}}}},
        {"id": "civicsite.flag_summary", "type": "esql",
         "description": "Counts of flags by reason code and severity across the whole indexed portfolio.",
         "tags": ["civicsite", "aggregation"],
         "configuration": {"query": f"FROM {f} | STATS flags = COUNT(*) BY reason_code, severity | SORT flags DESC | LIMIT 50",
                           "params": {}}},
        {"id": "civicsite.search_records", "type": "index_search",
         "description": "Search DOB filings, permits, violations and complaints by meaning or keywords "
                        "(semantic_text powered by Mistral embeddings + BM25).",
         "tags": ["civicsite", "search"],
         "configuration": {"pattern": r}},
    ]


def register(settings: Settings, log=print) -> dict:
    if not settings.kibana_url:
        raise RuntimeError("KIBANA_URL is not set in .env")
    headers = {"Authorization": f"ApiKey {settings.kibana_api_key}", "kbn-xsrf": "true",
               "Content-Type": "application/json"}
    idx = {"flags": settings.idx_flags, "records": settings.idx_records, "projects": settings.idx_projects}
    base = f"{settings.kibana_url}/api/agent_builder"
    created = []
    with httpx.Client(timeout=60, headers=headers) as c:
        for tool in _tools(idx):
            tid = tool["id"]
            ok = False
            for ptype in ("string", "keyword"):
                tool = _tools(idx, ptype)[[t["id"] for t in _tools(idx)].index(tid)]
                c.delete(f"{base}/tools/{tid}")
                r = c.post(f"{base}/tools", json=tool)
                if r.status_code < 300:
                    ok = True
                    break
                if r.status_code in (401, 403):
                    raise RuntimeError(f"Kibana auth failed ({r.status_code}): {r.text[:300]}. "
                                       "Create an API key with Kibana (Agent Builder) privileges -> KIBANA_API_KEY.")
            log(("registered tool " if ok else "!! FAILED tool ") + tid + ("" if ok else f": {r.status_code} {r.text[:300]}"))
            if ok:
                created.append(tid)
        agent = {
            "id": AGENT_ID, "name": "CivicSite Permit Copilot",
            "description": "Cited preflight action queue over NYC DOB filings, permits and violations.",
            "configuration": {"instructions": INSTRUCTIONS, "tools": [{"tool_ids": created}]},
        }
        c.delete(f"{base}/agents/{AGENT_ID}")
        r = c.post(f"{base}/agents", json=agent)
        log(("registered agent " if r.status_code < 300 else "!! FAILED agent: ") + (AGENT_ID if r.status_code < 300 else f"{r.status_code} {r.text[:300]}"))
    mcp = f"{settings.kibana_url}/api/agent_builder/mcp"
    log(f"MCP server: {mcp}  (auth header: 'Authorization: ApiKey <key>')")
    return {"tools": created, "agent": AGENT_ID, "mcp": mcp}
