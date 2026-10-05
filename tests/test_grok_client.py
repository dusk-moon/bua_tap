import json

import httpx
import pytest

from bua_tap.config import TAPConfig
from bua_tap.llm.grok_client import GrokClient, LLMError
from bua_tap.models import AttackerOutput, Message


def envelope(text='{"improvement":"First attempt","attack_string":"fixture"}'):
    return {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


def call(client):
    return client.generate(
        [Message(role="user", content="context")],
        AttackerOutput,
        model="test-model",
        temperature=None,
        max_tokens=256,
    )


def test_responses_request_and_strict_validation():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=envelope())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = call(GrokClient(TAPConfig(), api_key="test-key", http_client=http))
    payload = json.loads(requests[0].content)
    assert str(requests[0].url) == "https://api.x.ai/v1/responses"
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    assert payload["store"] is False
    assert result.attack_string == "fixture"


@pytest.mark.parametrize("mode", ["rate_limit", "server", "timeout"])
def test_bounded_exponential_retry(mode):
    attempts, delays = [], []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 4:
            if mode == "timeout":
                raise httpx.ReadTimeout("private transport detail", request=request)
            return httpx.Response(429 if mode == "rate_limit" else 503)
        return httpx.Response(200, json=envelope())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        call(GrokClient(
            TAPConfig(backoff_seconds=1.0, max_backoff_seconds=3.0),
            api_key="test-key",
            http_client=http,
            sleep=delays.append,
        ))
    assert len(attempts) == 4
    assert delays == [1, 2, 3]


def test_nonretryable_error_does_not_leak_response_body():
    def handler(request):
        return httpx.Response(401, text="private provider response")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(LLMError) as error:
            call(GrokClient(TAPConfig(), api_key="test-key", http_client=http))
    assert "private provider response" not in str(error.value)

