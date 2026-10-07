"""Offline build step: raw Socrata rows -> normalized records, flags and project summaries (JSONL)."""
from __future__ import annotations

import json
import time

from .checks import analyze_all
from .config import PROC_DIR, Settings
from .normalize import assign_semantic_text, normalize_all
from .socrata import DATASETS, fetch_dataset, read_raw


def fetch(settings: Settings, refresh: bool = False, log=print) -> None:
    for name in DATASETS:
        t = time.time()
        p = fetch_dataset(name, settings, refresh=refresh)
        with open(p, encoding="utf-8") as fh:
            n = sum(1 for _ in fh)
        log(f"{name:18s} {n:>8d} rows  ({time.time() - t:.0f}s)  {DATASETS[name].dataset_id}")


def _dump(path, rows) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            n += 1
    return n


def build(settings: Settings, log=print) -> dict:
    t = time.time()
    raw = {n: read_raw(n) for n in DATASETS}
    log("raw rows: " + ", ".join(f"{k}={len(v)}" for k, v in raw.items()))
    records = normalize_all(raw)
    flags, projects = analyze_all(records, settings.as_of)
    boost = {e["record_id"] for f in flags for e in f["evidence"]}
    n_sem = assign_semantic_text(records, settings.semantic_max_docs, boost)
    PROC_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "records": _dump(PROC_DIR / "records.jsonl", records),
        "flags": _dump(PROC_DIR / "flags.jsonl", flags),
        "projects": _dump(PROC_DIR / "projects.jsonl", projects),
        "semantic_docs": n_sem,
    }
    log(f"built in {time.time() - t:.0f}s as of {settings.as_of}: {out}")
    return out
