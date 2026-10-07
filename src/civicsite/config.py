"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROC_DIR = DATA_DIR / "processed"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _months_ago(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + (d.month - 1) - months, 12)
    return date(y, m + 1, 1)


@dataclass
class Settings:
    es_url: str = field(default_factory=lambda: _env("ELASTICSEARCH_URL", "http://localhost:9200"))
    es_api_key: str = field(default_factory=lambda: _env("ELASTIC_API_KEY"))
    kibana_url: str = field(default_factory=lambda: _env("KIBANA_URL").rstrip("/"))
    kibana_api_key: str = field(default_factory=lambda: _env("KIBANA_API_KEY") or _env("ELASTIC_API_KEY"))
    mistral_api_key: str = field(default_factory=lambda: _env("MISTRAL_API_KEY"))
    chat_model: str = field(default_factory=lambda: _env("MISTRAL_CHAT_MODEL", "mistral-large-4"))
    extract_model: str = field(default_factory=lambda: _env("MISTRAL_EXTRACT_MODEL", "mistral-small-latest"))
    embed_model: str = field(default_factory=lambda: _env("MISTRAL_EMBED_MODEL", "mistral-embed"))
    embed_inference_id: str = "civicsite-mistral-embeddings"
    chat_inference_id: str = "civicsite-mistral-chat"
    socrata_token: str = field(default_factory=lambda: _env("SOCRATA_APP_TOKEN"))
    window_months: int = field(default_factory=lambda: int(_env("WINDOW_MONTHS", "12")))
    violation_lookback_months: int = field(default_factory=lambda: int(_env("VIOLATION_LOOKBACK_MONTHS", "24")))
    semantic_max_docs: int = field(default_factory=lambda: int(_env("SEMANTIC_MAX_DOCS", "20000")))
    prefix: str = "civicsite"

    @property
    def as_of(self) -> date:
        raw = _env("AS_OF")
        return date.fromisoformat(raw) if raw else date.today()

    @property
    def filing_start(self) -> date:
        return _months_ago(self.as_of, self.window_months)

    @property
    def violation_start(self) -> date:
        return _months_ago(self.as_of, self.violation_lookback_months)

    @property
    def idx_records(self) -> str:
        return f"{self.prefix}_records"

    @property
    def idx_projects(self) -> str:
        return f"{self.prefix}_projects"

    @property
    def idx_flags(self) -> str:
        return f"{self.prefix}_flags"

    @property
    def idx_evidence(self) -> str:
        return f"{self.prefix}_evidence"


def get_settings() -> Settings:
    return Settings()
