import json

import pytest

from bua_tap.config import TAPConfig
from bua_tap.llm.evaluator import Evaluator
from bua_tap.llm.fixture import FixtureClient
from bua_tap.llm.grok_client import LLMError, parse_output
from bua_tap.models import EvaluationResult, Message
from bua_tap.rollout.mock import MockBUARolloutRunner


def valid_evaluation():
    return {"score": 5.0, "attack_progress_summary": "Some progress",
            "benign_task_summary": "Paused", "key_evidence": ["action 1"], "limiting_factor": "Incomplete"}


@pytest.mark.parametrize("changes", [{"score": -1}, {"score": 11}, {"score": "5"}, {"score": True},
    {"score": float("nan")}, {"success": False}, {"unknown": "field"},
    {"key_evidence": ["x" * 401]}, {"attack_progress_summary": "x" * 1201}])
def test_malformed_evaluation_rejected(changes):
    with pytest.raises(LLMError):
        parse_output(json.dumps(valid_evaluation() | changes), EvaluationResult)


@pytest.mark.parametrize("raw", ["```json\n{}\n```", "{}", "null", "[]", '{"score": 1, "score": 2}'])
def test_invalid_json_or_missing_fields(raw):
    with pytest.raises(LLMError):
        parse_output(raw, EvaluationResult)


def test_evaluator_receives_both_evidence_sources_and_context(context):
    class Capture(FixtureClient):
        def generate(self, messages, schema, **kwargs):
            self.messages = messages
            return super().generate(messages, schema, **kwargs)
    client = Capture()
    rollout = MockBUARolloutRunner().run("candidate", context)
    Evaluator(client, TAPConfig()).evaluate(context, "candidate", rollout)
    evidence = json.loads(client.messages[1].content)
    assert evidence["rollout"]["reasoning_trace"] == rollout.reasoning_trace
    assert evidence["rollout"]["action_log"] == [a.model_dump() for a in rollout.action_log]
    assert evidence["context"] == context.model_dump()
    assert "success" not in EvaluationResult.model_fields


def test_evaluator_compacts_framework_state_but_keeps_every_ordered_step(context):
    class Capture(FixtureClient):
        def generate(self, messages, schema, **kwargs):
            self.messages = messages
            return super().generate(messages, schema, **kwargs)

    client = Capture()
    rollout = MockBUARolloutRunner().run("candidate", context)
    rollout.trajectory = [
        {
            "step": 1,
            "state": {"screenshot_path": "large-local-state"},
            "state_message": "DOM" * 10000,
            "exposed_trace": {"next_goal": "Open labels"},
            "requested_actions": [{"click": {"index": 7}}],
            "action_results": [{"is_done": False}],
            "metadata": {"large": "metadata" * 10000},
        },
        {
            "step": 2,
            "state_message": "another full browser prompt",
            "exposed_trace": {"next_goal": "Apply TAP_TEST"},
            "requested_actions": [{"click": {"index": 8}}],
            "action_results": [{"is_done": True}],
        },
    ]

    Evaluator(client, TAPConfig()).evaluate(context, "candidate", rollout)

    evidence = json.loads(client.messages[1].content)["rollout"]
    assert [step["step"] for step in evidence["trajectory"]] == [1, 2]
    assert all("state_message" not in step and "state" not in step for step in evidence["trajectory"])
    assert evidence["trajectory"][1]["requested_actions"] == [{"click": {"index": 8}}]
