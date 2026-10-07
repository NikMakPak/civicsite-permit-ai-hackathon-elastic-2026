"""CivicSite command line: `civicsite --help`."""
from __future__ import annotations

import json
import os
import sys

import typer
from rich.console import Console
from rich.markup import escape
from rich.text import Text
from rich.panel import Panel
from rich.table import Table

from . import pipeline
from .config import get_settings

app = typer.Typer(add_completion=False, help="CivicSite Permit Copilot (Elasticsearch + Mistral over NYC DOB data)")
console = Console()
SEV_STYLE = {"high": "bold red", "medium": "yellow", "low": "cyan"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@app.command()
def fetch(refresh: bool = typer.Option(False, help="Re-download even if cached")):
    """1. Download the NYC Open Data slices (Manhattan, last 12 months) to data/raw."""
    pipeline.fetch(get_settings(), refresh, console.print)


@app.command()
def build():
    """2. Normalize records, run deterministic checks, write data/processed/*.jsonl."""
    pipeline.build(get_settings(), console.print)


@app.command()
def setup(recreate: bool = typer.Option(False, help="Drop and recreate indices"),
          no_semantic: bool = typer.Option(False, help="Skip Mistral semantic_text (BM25 only)")):
    """3. Create Mistral inference endpoints in Elasticsearch + index mappings."""
    from .es_setup import create_indices, ensure_inference_endpoints, get_es
    s = get_settings()
    es = get_es(s)
    console.print(f"connected to Elasticsearch {es.info()['version']['number']}")
    ok = {"embeddings": False}
    if not no_semantic:
        ok = ensure_inference_endpoints(es, s, console.print)
    create_indices(es, s, recreate=recreate, semantic=ok["embeddings"] and not no_semantic, log=console.print)


@app.command()
def index():
    """4. Bulk-index records (Mistral embeds semantic_text at ingest), flags and projects."""
    from .es_setup import get_es, index_all
    s = get_settings()
    index_all(get_es(s), s, console.print)


@app.command()
def status():
    """Show backend, window and index counts."""
    from .store import get_store
    s = get_settings()
    st = get_store(s, os.environ.get("CIVICSITE_BACKEND", "auto"))
    console.print({"backend": st.backend, "as_of": str(s.as_of), "stats": st.stats(),
                   "mistral_key": bool(s.mistral_api_key)})


def _store(backend: str):
    from .store import get_store
    return get_store(get_settings(), backend)


def _render(a: dict) -> None:
    p = a["property"]
    console.print(Panel.fit(
        f"[bold]{p['address']}[/]  BIN {a['bin']}  BBL {p['bbl']}  ZIP {p['zip']}\n"
        f"records: {p['counts']}  open violations: {p['open_violations']}  as of {a['as_of']}",
        title="CivicSite preflight"))
    if not a["flags"]:
        console.print("[green]No rule flags in the indexed window.[/]")
    for i, f in enumerate(a["flags"][:10], 1):
        console.print(f"[{SEV_STYLE[f['severity']]}]{i}. \[{f['severity'].upper()}][/] {escape(f['title'])}")
        console.print(f"   {f['reason_code']} | controlled by {f['controlled_by']} | confidence {f['confidence']} "
                      f"({f['join_basis']}) | score {f['score']}")
        console.print(f"   Why: {f['explanation']}")
        console.print(f"   Next: {f['recommended_action']} ({f['owner_hint']})")
        if f.get("caveat"):
            console.print(f"   [dim]Caveat: {f['caveat']}[/]")
        for e in f["evidence"]:
            console.print(f"   [dim]evidence[/] \[{escape(e['record_id'])}] {e['status']} {e['date']}  {escape(e['source_url'])}")
    t = Table(title="Timeline (latest 12)")
    for c in ("date", "type", "status", "summary"):
        t.add_column(c, overflow="fold")
    for e in a["timeline"][:12]:
        t.add_row(e["date"], e["type"], str(e["status"]), str(e["summary"]))
    console.print(t)


@app.command()
def check(query: str, backend: str = typer.Option("auto", help="auto|elastic|local")):
    """Preflight a building by address, BIN or BBL."""
    st = _store(backend)
    cands = st.resolve(query, 5)
    if not cands:
        console.print("[red]no building found[/]")
        raise typer.Exit(1)
    if len(cands) > 1:
        console.print("[dim]candidates:[/] " + "; ".join(f"{c['address']} ({c['bin']})" for c in cands))
    _render(st.analysis(cands[0]["bin"]))


@app.command()
def search(query: str, bin: str = typer.Option(None), record_type: str = typer.Option(None),
           status_group: str = typer.Option(None), backend: str = "auto"):
    """Hybrid (BM25 + Mistral semantic) search over all records."""
    r = _store(backend).search(query, bin, record_type, status_group, 8)
    console.print(f"[dim]mode: {r['mode']}[/]")
    for h in r["hits"]:
        console.print(f"[{h['record_id']}] {h.get('address')} | {h.get('status')} | {h.get('event_date')}\n   "
                      f"{(h.get('description') or h.get('summary') or '')[:160]}")


@app.command()
def ask(question: str, bin: str = typer.Option(None), backend: str = "auto", trace: bool = True):
    """Natural-language question; Mistral calls the Elasticsearch/rule tools."""
    from .agent import ask as run
    r = run(_store(backend), get_settings(), question, bin)
    if trace:
        for t in r["tool_trace"]:
            console.print(f"[dim]-> {t['tool']}({json.dumps(t['args'])}) : {t['summary']}[/]")
    console.print(Panel(Text(r["answer"]), title=f"answer ({r['model']}, {r['backend']})"))
    c = r["citations"]
    console.print(f"citations verified: {c['valid']}/{c['cited']}" + (f"  unverified: {c['invalid_ids']}" if c["invalid_ids"] else ""))


@app.command()
def enrich(limit: int = 40, backend: str = "auto"):
    """Pre-compute Mistral structured extractions for the evidence of the top flags (warms the cache)."""
    from .extract import extract_record
    s, st = get_settings(), _store(backend)
    top = st.portfolio(None, None, limit)["top"]
    n = 0
    for p in top:
        a = st.analysis(p["bin"])
        for f in a["flags"][:2]:
            rec = st.get_record(f["evidence"][0]["record_id"])
            if rec:
                d = extract_record(st, s, rec)
                n += 1
                console.print(Text(f"{rec['record_id']}: {d.get('extracted', {}).get('work_summary', d.get('skipped'))}"))
    console.print(f"extracted {n} records")


@app.command("agent-builder")
def agent_builder():
    """Register Elastic Agent Builder tools + agent (bonus) and print the MCP URL."""
    from .agent_builder import register
    register(get_settings(), console.print)


@app.command()
def eval(n: int = 100, labels: str = typer.Option(None, help="Path to filled labels CSV for precision")):
    """Citation-correctness vs live NYC Open Data + labeling template for precision."""
    from pathlib import Path

    from .evaluate import precision_from_labels, run_eval
    if labels:
        console.print(precision_from_labels(Path(labels)))
    else:
        run_eval(get_settings(), n, console.print)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, backend: str = "auto"):
    """Run the API + demo page."""
    import uvicorn
    os.environ["CIVICSITE_BACKEND"] = backend
    uvicorn.run("civicsite.api:app", host=host, port=port, log_level="info")


@app.command()
def all(refresh: bool = False):
    """Run fetch -> build -> setup -> index."""
    pipeline.fetch(get_settings(), refresh, console.print)
    pipeline.build(get_settings(), console.print)
    setup(recreate=True, no_semantic=False)
    index()


if __name__ == "__main__":
    app()
