"""Single stateless xAI Responses transport with strict local validation."""
import json
import os
import time
from typing import Protocol, TypeVar

import httpx
from pydantic import ValidationError

from ..config import TAPConfig
from ..models import Message, StrictModel

T = TypeVar("T", bound=StrictModel)


class LLMError(RuntimeError):
    """Sanitized transport or output error; never includes request headers/body."""


class StructuredClient(Protocol):
    def generate(self, messages: list[Message], schema: type[T], *, model: str,
                 temperature: float | None, max_tokens: int) -> T: ...


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def parse_output(text: str, schema: type[T]) -> T:
    try:
        # No fence stripping, coercion, implicit repair, or default success values.
        data = json.loads(text, object_pairs_hook=_unique_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
        return schema.model_validate(data)
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors(include_input=False))
        raise LLMError(f"Invalid {schema.__name__} output; invalid fields: {fields}") from None
    except (ValueError, TypeError):
        raise LLMError(f"Invalid {schema.__name__} output; expected one strict JSON object") from None


class GrokClient:
    def __init__(self, config: TAPConfig, *, api_key: str | None = None,
                 http_client: httpx.Client | None = None, sleep=time.sleep):
        self.config = config
        self._key = api_key or os.environ.get("XAI_API_KEY")
        if not self._key:
            raise LLMError("Set XAI_API_KEY for live Grok calls, or use --llm-backend fixture.")
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client()
        self._sleep = sleep

    def close(self):
        if self._owns_client:
            self._http.close()

    def generate(self, messages: list[Message], schema: type[T], *, model: str,
                 temperature: float | None, max_tokens: int) -> T:
        payload = {
            "model": model,
            "input": [message.model_dump() for message in messages],
            "store": False,
            "max_output_tokens": max_tokens,
            "text": {"format": {"type": "json_schema", "name": schema.__name__,
                                "strict": True, "schema": schema.model_json_schema()}},
        }
        if temperature is not None:
            payload["temperature"] = temperature
        response = None
        for attempt in range(self.config.max_retries + 1):
            retryable = False
            try:
                response = self._http.post(
                    "https://api.x.ai/v1/responses", json=payload,
                    headers={"Authorization": f"Bearer {self._key}"},
                    timeout=self.config.timeout_seconds,
                )
                if response.is_success:
                    break
                retryable = response.status_code in (408, 429) or response.status_code >= 500
                error = f"xAI HTTP {response.status_code} for model {model}"
            except httpx.TransportError:
                retryable = True
                error = f"xAI transport/timeout error for model {model}"
            if not retryable or attempt == self.config.max_retries:
                raise LLMError(error) from None
            delay = min(self.config.max_backoff_seconds, self.config.backoff_seconds * (2 ** attempt))
            self._sleep(delay)
        try:
            body = response.json()
            if body.get("status") != "completed" or body.get("error"):
                raise LLMError("xAI response incomplete or failed; no candidate/evaluation accepted")
            texts = []
            for item in body["output"]:
                if item.get("type") != "message":
                    continue
                for part in item.get("content", []):
                    if part.get("type") == "refusal":
                        raise LLMError("xAI refused structured generation")
                    if part.get("type") == "output_text":
                        texts.append(part["text"])
            if not texts:
                raise LLMError("xAI response contains no output text")
            return parse_output("".join(texts), schema)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise LLMError("Malformed xAI response envelope") from None
