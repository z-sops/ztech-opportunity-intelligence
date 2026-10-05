"""LLM abstraction (spec #25).

The LLM is used ONLY to label / classify / resolve text that a provider has
already observed. Its output is always stored as claim_kind=INFERENCE and its
self-reported confidence is capped. With no LLM configured the engine falls
back to deterministic rules — it never needs an LLM to produce facts.

`OpenAICompatibleLLM` speaks the /chat/completions protocol, so it works with
OpenAI, OpenRouter, Groq, Together, DeepSeek, local vLLM/Ollama gateways etc.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from ..domain.errors import FetchError
from ..domain.taxonomy import ErrorCode
from ..net.http import SafeHttpClient

LLM_CONFIDENCE_CAP = 0.75


class LLMClient(Protocol):
    name: str
    configured: bool

    async def json_completion(self, system: str, user: str, *, max_tokens: int = 800) -> dict[str, Any] | None: ...


class NullLLM:
    name = "none"
    configured = False

    async def json_completion(self, system: str, user: str, *, max_tokens: int = 800) -> dict[str, Any] | None:
        return None


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Defensive JSON extraction from an LLM reply."""
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(cleaned[start : end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


class OpenAICompatibleLLM:
    name = "openai_compatible"
    configured = True

    def __init__(self, http: SafeHttpClient, *, api_key: str, base_url: str, model: str) -> None:
        self._http = http
        self._key = api_key
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model

    async def json_completion(self, system: str, user: str, *, max_tokens: int = 800) -> dict[str, Any] | None:
        body = {
            "model": self._model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system + "\nReply with ONE valid JSON object only."},
                {"role": "user", "content": user[:12000]},
            ],
        }
        resp = await self._http.post_json(self._url, body, headers={"Authorization": f"Bearer {self._key}"})
        if resp.status in (401, 403):
            raise FetchError(ErrorCode.PROVIDER_NOT_CONFIGURED, "LLM endpoint rejected the API key")
        if not resp.ok:
            raise FetchError(ErrorCode.SOURCE_UNAVAILABLE, f"LLM HTTP {resp.status}")
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        return parse_json_object(content if isinstance(content, str) else "")
