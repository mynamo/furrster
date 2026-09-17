"""Anthropic client wrapper.

Everything LLM-shaped goes through here so there is one place that handles the
API key, the model name, retries, and JSON parsing. Import it lazily: the ingest
and scoring paths must keep working with no key set.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .config import Settings

log = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, settings: Settings) -> None:
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMError("pip install anthropic to use matching / bio drafting") from exc
        self._client = Anthropic(api_key=settings.require_anthropic())
        self._model = settings.anthropic_model

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    def complete_json(self, *, system: str, user: str, **kwargs: Any) -> Any:
        """Ask for JSON and actually get JSON back, prose fences and all."""
        text = self.complete(system=system, user=user, **kwargs).strip()
        return parse_json(text)


def parse_json(text: str) -> Any:
    candidates = [text]
    block = _JSON_BLOCK.search(text)
    if block:
        candidates.insert(0, block.group(1))

    # Last resort: the outermost {...} or [...] in the response. Try whichever
    # bracket opens first, so prose around an object containing an array does not
    # get mistaken for a top-level array.
    spans = []
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            spans.append((start, -end, text[start : end + 1]))
    candidates.extend(snippet for _, _, snippet in sorted(spans))

    for candidate in candidates:
        try:
            return json.loads(candidate.strip())
        except json.JSONDecodeError:
            continue
    raise LLMError(f"Model did not return parseable JSON. Got: {text[:300]}")
