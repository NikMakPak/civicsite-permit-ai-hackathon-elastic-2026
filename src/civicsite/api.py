"""FastAPI service: the CivicSite backend + a one-page demo UI."""
from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent import ask as run_agent
from .config import get_settings
from .extract import extract_record
from .llm import LLMUnavailable
from .store import get_store, slim

STATIC = Path(__file__).parent / "static"
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    state["s"] = s
    state["store"] = get_store(s, os.environ.get("CIVICSITE_BACKEND", "auto"))
    yield


app = FastAPI(title="CivicSite Permit Copilot", version="0.1.0", lifespan=lifespan)


class AskBody(BaseModel):
    question: str
    bin: str | None = None


class ExtractBody(BaseModel):
    record_id: str


@app.get("/api/config")
def config():
    s, st = state["s"], state["store"]
    return {"backend": st.backend, "as_of": s.as_of.isoformat(), "llm_enabled": bool(s.mistral_api_key),
            "chat_model": s.chat_model, "extract_model": s.extract_model,
            "window": {"filings_from": s.filing_start.isoformat(), "violations_from": s.violation_start.isoformat()},
            "stats": st.stats()}


@app.get("/api/resolve")
def resolve(q: str = Query(min_length=2)):
    return {"candidates": state["store"].resolve(q, 8)}


@app.get("/api/property")
def prop(q: str | None = None, bin: str | None = None):
    st = state["store"]
    if not bin:
        if not q:
            raise HTTPException(400, "q or bin required")
        cands = st.resolve(q, 8)
        if not cands:
            return {"candidates": [], "analysis": None}
        if len(cands) > 1 and not re.fullmatch(r"\d{7}|\d{10}", q.strip()) and cands[0].get("_score", 0) is not None:
            # ambiguous address -> let the UI pick unless the top hit is clearly best
            top, second = cands[0], cands[1]
            if (top.get("_score") or 0) < (second.get("_score") or 0) * 1.25 and top["address"] != second["address"]:
                return {"candidates": cands, "analysis": None}
        bin = cands[0]["bin"]
    a = st.analysis(bin)
    return {"candidates": [], "analysis": a if a["found"] else None}


@app.get("/api/search")
def search(q: str = Query(min_length=2), bin: str | None = None, record_type: str | None = None,
           status_group: str | None = None, size: int = 8):
    return state["store"].search(q, bin, record_type, status_group, size)


@app.get("/api/portfolio")
def portfolio(reason: str | None = None, zip: str | None = None, limit: int = 10):
    return state["store"].portfolio(reason, zip, min(limit, 50))


@app.post("/api/ask")
def ask(body: AskBody):
    try:
        return run_agent(state["store"], state["s"], body.question, body.bin)
    except LLMUnavailable as e:
        raise HTTPException(503, str(e)) from e


@app.post("/api/extract")
def extract(body: ExtractBody):
    st = state["store"]
    rec = st.get_record(body.record_id)
    if not rec:
        raise HTTPException(404, "unknown record_id")
    try:
        return extract_record(st, state["s"], rec) | {"record": slim(rec)}
    except LLMUnavailable as e:
        raise HTTPException(503, str(e)) from e


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
