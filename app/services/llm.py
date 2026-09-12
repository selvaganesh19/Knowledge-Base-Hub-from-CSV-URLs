"""LLM providers.

Using httpx directly rather than a vendor SDK: the request shape is simple enough
to be its own documentation, and it avoids pinning to SDK versions that churn.

Every failure mode - missing key, unreachable host, exhausted free-tier quota,
malformed response - collapses into the same behaviour. `get_provider()` falls
through Groq, then Gemini, then a Null provider, and callers treat the Null case
as "return retrieval results without a narrative" rather than as an error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Free tiers throttle aggressively; one retry with a short backoff covers the
# common transient 429 without making a failing call feel slow.
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAY_SECONDS = 2.0


class LLMUnavailable(RuntimeError):
    """Raised when no provider can serve a request."""


class LLMHttpError(Exception):
    """An HTTP error from a provider, carrying enough detail to react to it.

    Kept separate from LLMUnavailable so a caller can tell "this specific model
    is gone, try the next one" apart from "the provider is unusable".
    """

    def __init__(self, provider: str, status: int, text: str) -> None:
        self.provider = provider
        self.status = status
        self.text = text
        super().__init__(f"{provider} returned HTTP {status}: {text[:300]}")

    @property
    def is_model_unavailable(self) -> bool:
        """True when the failure looks like a retired or renamed model.

        Groq retires models regularly, and a stale name fails the whole request
        with a 400/404 that mentions the model. Detecting it here is what lets the
        provider fall through to another model instead of failing the job.
        """
        if self.status not in (400, 404):
            return False
        lowered = self.text.lower()
        return "model" in lowered and any(
            hint in lowered
            for hint in (
                "decommission",
                "not found",
                "does not exist",
                "no longer",
                "invalid model",
            )
        )


class LLMProvider:
    """Base class. Subclasses implement available() and complete()."""

    name = "null"

    def available(self) -> bool:
        return False

    async def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        raise LLMUnavailable(f"provider '{self.name}' is not available")


class NullProvider(LLMProvider):
    """Used when no API key is configured. Degrades rather than fails."""

    name = "none"

    def __init__(self, reason: str = "no LLM API key configured") -> None:
        self.reason = reason

    async def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        raise LLMUnavailable(self.reason)


class GroqProvider(LLMProvider):
    """Groq's OpenAI-compatible chat completions endpoint.

    Holds an ordered list of candidate models. If the configured one has been
    retired, the next is tried and the working model is remembered for the rest of
    the process, so one deprecation does not take the pipeline down.
    """

    name = "groq"

    def __init__(self, api_key: str, model: str, fallbacks: list[str] | None = None) -> None:
        self.api_key = api_key
        self.models = [model, *(fallbacks or [])]
        self._active = model

    def available(self) -> bool:
        return bool(self.api_key)

    def _candidates(self) -> list[str]:
        # The last known-good model first, then the rest in configured order.
        ordered = [self._active, *self.models]
        seen: list[str] = []
        for model in ordered:
            if model and model not in seen:
                seen.append(model)
        return seen

    async def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        candidates = self._candidates()
        last_error: Exception | None = None

        for index, model in enumerate(candidates):
            payload: dict = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
                "max_tokens": 2048,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}

            try:
                data = await _post_with_retry(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    payload=payload,
                    provider=self.name,
                )
            except LLMHttpError as exc:
                last_error = exc
                if exc.is_model_unavailable and index < len(candidates) - 1:
                    logger.warning(
                        "groq model %s unavailable, trying %s next (%s)",
                        model,
                        candidates[index + 1],
                        exc.text[:120],
                    )
                    continue
                raise LLMUnavailable(str(exc)) from exc

            self._active = model
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMUnavailable(f"unexpected Groq response shape: {exc}") from exc

        raise LLMUnavailable(f"no usable Groq model: {last_error}")


class GeminiProvider(LLMProvider):
    """Google Gemini generateContent."""

    name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def available(self) -> bool:
        return bool(self.api_key)

    async def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        url = GEMINI_URL.format(model=self.model)
        payload: dict = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
        }
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        try:
            data = await _post_with_retry(
                url,
                headers={"x-goog-api-key": self.api_key},
                payload=payload,
                provider=self.name,
            )
        except LLMHttpError as exc:
            raise LLMUnavailable(str(exc)) from exc

        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable(f"unexpected Gemini response shape: {exc}") from exc


async def _post_with_retry(url: str, headers: dict, payload: dict, provider: str) -> dict:
    last_error = ""

    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
                response = await client.post(url, headers=headers, json=payload)

            if response.status_code in RETRY_STATUSES and attempt == 0:
                last_error = f"HTTP {response.status_code}"
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue

            if response.status_code >= 400:
                raise LLMHttpError(provider, response.status_code, response.text)

            return response.json()

        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == 0:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue
            raise LLMUnavailable(f"{provider} request failed: {last_error}") from exc

    raise LLMUnavailable(f"{provider} request failed: {last_error}")


def get_provider() -> LLMProvider:
    """Resolve the configured provider, falling through to Null if none will work."""
    configured = (settings.llm_provider or "auto").strip().lower()

    groq = GroqProvider(
        settings.groq_api_key,
        settings.groq_model,
        fallbacks=settings.groq_fallback_model_list(),
    )
    gemini = GeminiProvider(settings.gemini_api_key, settings.gemini_model)

    if configured == "none":
        return NullProvider("LLM_PROVIDER is set to 'none'")
    if configured == "groq":
        return groq if groq.available() else NullProvider("GROQ_API_KEY is not set")
    if configured == "gemini":
        return gemini if gemini.available() else NullProvider("GEMINI_API_KEY is not set")

    # auto
    if groq.available():
        return groq
    if gemini.available():
        return gemini
    return NullProvider()


def parse_json_response(text: str) -> dict:
    """Parse a JSON object out of a model response.

    Models wrap JSON in code fences or add a sentence of preamble often enough
    that stripping fences and falling back to a brace scan is worth the few lines.
    """
    cleaned = text.strip()

    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"could not parse JSON from model response: {exc}") from exc

    raise LLMUnavailable("model response contained no JSON object")
