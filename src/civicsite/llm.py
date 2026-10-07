"""Thin wrapper around the Mistral SDK: client factory + model fallback."""
from __future__ import annotations

from mistralai.client import Mistral

from .config import Settings

FALLBACK_CHAT = ["mistral-large-latest", "mistral-medium-latest", "mistral-small-latest"]


class LLMUnavailable(RuntimeError):
    pass


def get_client(settings: Settings) -> Mistral:
    if not settings.mistral_api_key:
        raise LLMUnavailable("MISTRAL_API_KEY is not set (see .env.example)")
    return Mistral(api_key=settings.mistral_api_key)


def _is_model_error(e: Exception) -> bool:
    msg = str(e).lower()
    code = getattr(e, "status_code", None)
    return code in (400, 404) and ("model" in msg or "not found" in msg or "invalid" in msg)


def candidates(primary: str) -> list[str]:
    seen, out = set(), []
    for m in [primary, *FALLBACK_CHAT]:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def complete(client: Mistral, model: str, **kw):
    """chat.complete with fallback to sibling models if the configured id is unknown to the API."""
    last: Exception | None = None
    for m in candidates(model):
        try:
            return m, client.chat.complete(model=m, **kw)
        except Exception as e:  # noqa: BLE001
            last = e
            if not _is_model_error(e):
                raise
    raise last  # type: ignore[misc]


def parse(client: Mistral, model: str, response_format, **kw):
    last: Exception | None = None
    for m in candidates(model):
        try:
            return m, client.chat.parse(model=m, response_format=response_format, **kw)
        except Exception as e:  # noqa: BLE001
            last = e
            if not _is_model_error(e):
                raise
    raise last  # type: ignore[misc]
