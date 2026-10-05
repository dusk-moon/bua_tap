import asyncio
from copy import deepcopy
import json

import pytest

from bua_tap.rollout.browser_use_agent import BrowserUseAgent, history_result
from bua_tap.rollout.config import BrowserUseConfig
from bua_tap.rollout.errors import RolloutError


class History:
    usage = None
    def __init__(self, done=True):
        self.done = done
        self.history = [{"model_output": {"memory": "Read the note", "next_goal": "Summarize",
                          "action": [{"click": {"index": 1}}, {"input": {"text": "not executed"}}]},
                         "result": [{"extracted_content": "summary", "error": None}],
                         "state": {"url": "https://example.test"}}]
    def model_dump(self): return {"history": deepcopy(self.history)}
    def final_result(self): return "summary"
    def is_successful(self): return True if self.done else None
    def is_done(self): return self.done
    def errors(self): return []


def test_normalized_history_keeps_requested_and_executed_distinct():
    result, raw = history_result(History(), "completed")
    assert len(result.trajectory[0]["requested_actions"]) == 2
    assert len(result.action_log) == 1
    assert "Read the note" in result.reasoning_trace
    assert result.agent_reported_success is True
    assert result.final_response == "summary"


@pytest.mark.parametrize("mode", ["completed", "error", "timeout", "max_steps"])
def test_adapter_with_fake_agent_only(tmp_path, context, mode):
    events, tasks = [], []
    class Browser:
        async def stop(self): events.append("stop")
    class Agent:
        history = History(done=mode == "completed")
        async def run(self, *, max_steps, on_step_end):
            await on_step_end(self)
            if mode == "error": raise RuntimeError("private error")
            if mode == "timeout": await asyncio.sleep(10)
            return self.history
    def factory(task, config, cdp_url, directory):
        tasks.append(task)
        return Agent(), Browser()
    adapter = BrowserUseAgent(BrowserUseConfig(max_steps=1, rollout_timeout_seconds=0.02),
                               "http://127.0.0.1:9222", agent_factory=factory)
    if mode in ("error", "timeout"):
        with pytest.raises(RolloutError) as exc:
            asyncio.run(adapter.run(context, tmp_path))
        assert exc.value.result.termination_reason == mode
    else:
        result = asyncio.run(adapter.run(context, tmp_path))
        assert result.termination_reason == mode
    assert events == ["stop"]
    assert tasks == [context.benign_instruction]
    assert json.loads((tmp_path / "agent_history.json").read_text())["history"]


def test_openai_key_redacted_from_artifacts(tmp_path, monkeypatch):
    from bua_tap.rollout.artifacts import write_artifact
    monkeypatch.setenv("OPENAI_API_KEY", "private-openai-test-key")
    write_artifact(tmp_path / "test.json", {"content": "private-openai-test-key"})
    assert "private-openai-test-key" not in (tmp_path / "test.json").read_text()
