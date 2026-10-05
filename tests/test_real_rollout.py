import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from bua_tap.config import TAPConfig
from bua_tap.models import RolloutResult
from bua_tap.rollout.browser import ChromiumSession
from bua_tap.rollout.config import ChromiumConfig, RealRolloutConfig, SurfaceConfig
from bua_tap.rollout.errors import RolloutError
from bua_tap.rollout.real import RealBUARolloutRunner
from bua_tap.rollout.scripts import SurfaceScripts


@pytest.fixture
def rollout_config(tmp_path):
    return RealRolloutConfig(browser=ChromiumConfig(executable_path="chrome.exe",
                                                     user_data_dir=str(tmp_path / "profile")),
                             surface=SurfaceConfig(setup_script="pass", cleanup_script="pass"))


class Session:
    def __init__(self, events):
        self.events = events
    async def start(self): self.events.append("start")
    async def neutral_page(self):
        self.events.append("neutral")
        return object()
    async def snapshot(self):
        self.events.append("snapshot")
        return {"before_cleanup": True}
    async def close(self): self.events.append("close")


class Scripts:
    def __init__(self, events, fail=None, success_results=None):
        self.events, self.fail = events, fail
        self.success_results = list(success_results or [False])
    async def execute(self, name, page, candidate):
        self.events.append(name)
        if name == self.fail:
            raise ValueError("private failure text")
    async def check_success(self, page, candidate):
        from bua_tap.models import GroundTruthResult
        self.events.append("success_check")
        if self.fail == "success_check":
            raise ValueError("private failure text")
        success = self.success_results.pop(0) if len(self.success_results) > 1 else self.success_results[0]
        return GroundTruthResult(
            success=success,
            summary="Target element is visible." if success else "Target element is absent.",
            evidence={"candidate": candidate},
        )


class Agent:
    def __init__(self, events, fail=False): self.events, self.fail = events, fail
    async def run(self, context, directory):
        self.events.append("agent")
        result = RolloutResult(reasoning_trace="partial trace", action_log=[], termination_reason="completed")
        if self.fail:
            raise RolloutError("failure", result)
        return result


def make_runner(config, tmp_path, *, fail_script=None, fail_agent=False, status=None,
                success_results=None):
    events = []
    runner = RealBUARolloutRunner(config, tmp_path / "artifacts", session=Session(events),
        scripts=Scripts(events, fail_script, success_results),
        agent=Agent(events, fail_agent), status=status)
    return runner, events


def test_order_and_browser_reused_with_sync_facade(rollout_config, tmp_path, context):
    runner, events = make_runner(rollout_config, tmp_path)
    with runner:
        first = runner.run("one", context)
        runner.run("two", context)
    assert events.count("start") == 1
    assert events.count("agent") == events.count("setup") == events.count("cleanup") == 2
    assert events.index("setup") < events.index("agent") < events.index("snapshot") < events.index("cleanup")
    assert first.final_state == {"before_cleanup": True}
    assert first.lifecycle["cleanup"] == "verified"
    assert events[-1] == "close"


def test_rollout_status_describes_setup_bua_evidence_and_cleanup(rollout_config, tmp_path, context):
    messages = []
    runner, _ = make_runner(rollout_config, tmp_path, status=messages.append)
    with runner:
        runner.run("candidate", context)
    text = "\n".join(messages)
    assert "Launching Chromium" in text
    assert "injection surface are ready" in text
    assert "Rollout setup started" in text
    assert "Rollout setup verified" in text
    assert "BUA started" in text
    assert "BUA finished" in text
    assert "Capturing pre-cleanup browser evidence" in text
    assert "Rollout cleanup started" in text
    assert "Rollout cleanup verified" in text


def test_browser_success_check_runs_before_cleanup_and_is_saved(rollout_config, tmp_path, context):
    rollout_config.surface.success_check_script = "return True"
    messages = []
    runner, events = make_runner(
        rollout_config, tmp_path, status=messages.append, success_results=[False, True]
    )
    with runner:
        result = runner.run("candidate", context)
    checks = [index for index, event in enumerate(events) if event == "success_check"]
    assert len(checks) == 2
    assert checks[0] < events.index("agent") < events.index("snapshot") < checks[1] < events.index("cleanup")
    assert result.ground_truth.success is True
    assert result.lifecycle["success_check"] == "verified"
    assert "Ground-truth success check complete: success=True" in "\n".join(messages)


def test_browser_success_check_still_runs_after_bua_failure(rollout_config, tmp_path, context):
    rollout_config.surface.success_check_script = "return True"
    runner, events = make_runner(
        rollout_config, tmp_path, fail_agent=True, success_results=[False, True]
    )
    with runner:
        with pytest.raises(RolloutError) as error:
            runner.run("candidate", context)
    checks = [index for index, event in enumerate(events) if event == "success_check"]
    assert len(checks) == 2
    assert checks[0] < events.index("agent") < checks[1] < events.index("cleanup")
    assert error.value.result.ground_truth.success is True
    assert error.value.result.lifecycle["success_check"] == "verified"


def test_setup_contamination_skips_bua_and_cannot_count_as_success(
        rollout_config, tmp_path, context):
    rollout_config.surface.success_check_script = "return True"
    messages = []
    runner, events = make_runner(
        rollout_config, tmp_path, status=messages.append, success_results=[True]
    )
    with runner:
        result = runner.run("candidate containing /due 2026-09-29", context)
    assert "agent" not in events
    assert events.count("success_check") == 1
    assert result.termination_reason == "setup_contamination"
    assert result.lifecycle["setup_success_check"] == "contaminated"
    assert result.lifecycle["bua"] == "skipped_setup_contamination"
    assert result.lifecycle["success_check"] == "rejected_setup_contamination"
    assert result.ground_truth.success is False
    assert result.ground_truth.evidence["classification"] == "setup_contamination"
    assert result.ground_truth.evidence["setup_ground_truth"]["success"] is True
    assert result.metadata["setup_ground_truth"]["success"] is True
    assert events[-2] == "cleanup"
    assert "Setup contamination detected" in "\n".join(messages)


@pytest.mark.parametrize("fail_script,fail_agent", [("setup", False), (None, True), ("cleanup", False)])
def test_failures_cleanup_preserve_evidence_and_stop(rollout_config, tmp_path, context, fail_script, fail_agent):
    runner, events = make_runner(rollout_config, tmp_path, fail_script=fail_script, fail_agent=fail_agent)
    with runner:
        with pytest.raises(RolloutError) as error:
            runner.run("candidate", context)
        assert "cleanup" in events
        if fail_script == "setup":
            assert "agent" not in events
        if fail_agent:
            assert error.value.result.reasoning_trace == "partial trace"
        with pytest.raises(RolloutError, match="unusable"):
            runner.run("next", context)
    logs = list((tmp_path / "artifacts").glob("*/rollout.json"))
    assert len(logs) == 1
    assert "private failure text" not in logs[0].read_text()


def test_surface_only_never_calls_bua(rollout_config, tmp_path):
    runner, events = make_runner(rollout_config, tmp_path)
    with runner:
        result = runner.test_surface("literal candidate")
    assert "agent" not in events
    assert result.termination_reason == "surface_test_only"
    assert result.lifecycle["bua"] == "skipped"


def test_cancellation_still_cleans(rollout_config, tmp_path, context):
    runner, events = make_runner(rollout_config, tmp_path)
    async def cancelled(*args): raise asyncio.CancelledError()
    runner.agent.run = cancelled
    async def exercise():
        try:
            with pytest.raises(asyncio.CancelledError):
                await runner.arun("candidate", context)
            assert "cleanup" in events
        finally:
            await runner.aclose()
    asyncio.run(exercise())


def test_readiness_failure_closes_without_mutation(rollout_config, tmp_path):
    runner, events = make_runner(rollout_config, tmp_path, fail_script="readiness")
    try:
        with pytest.raises(RolloutError, match="readiness failed"): runner.start()
    finally:
        runner.close()
    assert "close" in events
    assert "setup" not in events
    assert (tmp_path / "artifacts" / "startup_failure.json").exists()


def test_config_requires_user_data_dir_and_compiles_python():
    with pytest.raises(ValidationError): ChromiumConfig()
    with pytest.raises(ValidationError): SurfaceConfig(setup_script="await page.foo( ;", cleanup_script="pass")


def test_literal_candidate_not_interpolated(tmp_path):
    class Page:
        async def fill(self, value): self.value = value
    page = Page()
    candidate = "\"'\n); raise Exception('not code')\n{attack_string} 🧪"
    scripts = SurfaceScripts(SurfaceConfig(setup_script="await page.fill(attack_string)", cleanup_script="pass"))
    asyncio.run(scripts.execute("setup", page, candidate))
    assert page.value == candidate


def test_script_timeout():
    scripts = SurfaceScripts(SurfaceConfig(setup_script="import asyncio\nawait asyncio.sleep(10)",
                                          cleanup_script="pass", script_timeout_seconds=0.01))
    with pytest.raises(TimeoutError): asyncio.run(scripts.execute("setup", None, "candidate"))


def test_script_assertion_preserves_diagnostic():
    from bua_tap.rollout.scripts import SurfaceScriptError
    scripts = SurfaceScripts(SurfaceConfig(setup_script="assert False, 'saved value differs'", cleanup_script="pass"))
    with pytest.raises(SurfaceScriptError) as error:
        asyncio.run(scripts.execute("setup", None, "candidate"))
    assert "saved value differs" in error.value.details


def test_success_check_requires_structured_ground_truth():
    scripts = SurfaceScripts(SurfaceConfig(
        setup_script="pass", cleanup_script="pass",
        success_check_script="return {'success': True, 'summary': 'Target exists', 'evidence': {'count': 1}}",
    ))
    result = asyncio.run(scripts.check_success(None, "candidate"))
    assert result.success is True
    assert result.evidence == {"count": 1}

    invalid = SurfaceScripts(SurfaceConfig(
        setup_script="pass", cleanup_script="pass", success_check_script="return None",
    ))
    with pytest.raises(RolloutError):
        asyncio.run(invalid.check_success(None, "candidate"))


def test_profile_resolution_and_browser_flags(tmp_path):
    root = tmp_path / "User Data"
    profile = root / "Profile 1"
    profile.mkdir(parents=True)
    (root / "Local State").write_text("{}")
    (profile / "Preferences").write_text("{}")
    config = ChromiumConfig(executable_path="chrome.exe", user_data_dir=str(profile) + " ")
    assert config.resolved_profile() == (root.resolve(), "Profile 1")
    command = ChromiumSession(config).launch_command()
    assert f"--user-data-dir={root.resolve()}" in command
    assert "--profile-directory=Profile 1" in command
    assert "--remote-debugging-address=127.0.0.1" in command
    assert "--headless=new" not in command


def test_gitlab_configuration_loads_and_gpt4o():
    config = TAPConfig.model_validate_json(Path("examples/gitlab_config.json").read_text())
    assert config.rollout.bua.model == "gpt-4o"
    assert config.rollout.browser.user_data_dir.endswith("Profile 1")
    assert "await editor.fill(expected)" in config.rollout.surface.setup_script
    assert 'expected = ""' in config.rollout.surface.cleanup_script
    assert "page.reload" in config.rollout.surface.cleanup_script
