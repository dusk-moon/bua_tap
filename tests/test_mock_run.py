import json

import pytest

from bua_tap.cli import main
from bua_tap.config import TAPConfig
from bua_tap.llm.fixture import FixtureClient
from bua_tap.models import GroundTruthResult
from bua_tap.rollout.mock import MockBUARolloutRunner
from bua_tap.rollout.real import RealBUARolloutRunner


class FourthRolloutSucceeds(MockBUARolloutRunner):
    def __init__(self):
        self.calls = 0

    def run(self, attack_string, context):
        self.calls += 1
        result = super().run(attack_string, context)
        result.ground_truth = GroundTruthResult(
            success=self.calls == 4,
            summary="Synthetic browser ground truth.",
            evidence={"call": self.calls},
        )
        return result


def test_early_success_returns_path(run_search):
    result, client, _ = run_search(runner=FourthRolloutSucceeds(), branching_factor=3, width=2, depth=5)
    assert result.stop_reason == "success"
    assert client.attacker_calls == client.evaluator_calls == 4
    assert [node.depth for node in result.path()] == [0, 1, 2]
    assert result.path()[-1].id == result.successful_node_id


def test_only_browser_ground_truth_controls_success():
    without_check = MockBUARolloutRunner().run("candidate", None)
    assert not TAPConfig().is_success(without_check)
    without_check.ground_truth = GroundTruthResult(
        success=True, summary="Observed target state.", evidence={}
    )
    assert TAPConfig().is_success(without_check)


def test_multidepth_mock_logs_raw_rollouts_and_manifest(run_search):
    result, _, logger = run_search(branching_factor=3, width=2, depth=3)
    records = [json.loads(line) for line in (logger.directory / "events.jsonl").read_text().splitlines()]
    evaluated = [r for r in records if r.get("stage") == "evaluated"]
    assert len(evaluated) == 15
    assert evaluated[0]["node"]["rollout_result"]["reasoning_trace"]
    assert evaluated[0]["node"]["rollout_result"]["action_log"]
    assert (logger.directory / "config.json").exists()
    assert json.loads((logger.directory / "result.json").read_text())["stop_reason"] == "max_depth"


def test_cli_offline_without_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert main(["--context", "examples/example_context.json", "--config", "examples/default_config.json",
                 "--llm-backend", "fixture", "--output-dir", str(tmp_path)]) == 0
    assert "evaluated=15" in capsys.readouterr().out


def test_cli_real_fails_before_credentials_or_generation(monkeypatch, capsys):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["--context", "examples/example_context.json", "--rollout-runner", "real"])
    assert exc.value.code == 2
    assert "rollout object" in capsys.readouterr().err


def test_failure_preserves_raw_rollout(tmp_path, context):
    from bua_tap.llm.attacker import Attacker
    from bua_tap.logging.experiment_logger import ExperimentLogger
    from bua_tap.rollout.mock import MockBUARolloutRunner
    from bua_tap.tap.search import TAPSearch

    class BrokenEvaluator:
        def evaluate(self, *args):
            raise RuntimeError("sensitive data in exception")
    config = TAPConfig(depth=1)
    logger = ExperimentLogger(tmp_path, context, config)
    search = TAPSearch(config, Attacker(FixtureClient(), config), BrokenEvaluator(), MockBUARolloutRunner(), logger)
    with pytest.raises(RuntimeError):
        search.run(context)
    log = (logger.directory / "events.jsonl").read_text()
    assert '"stage": "rollout_complete"' in log
    assert '"event": "run_failed"' in log
    assert "sensitive data" not in log


def test_key_redaction_in_all_artifacts(tmp_path, context, monkeypatch):
    from bua_tap.logging.experiment_logger import ExperimentLogger
    secret = "test-key-sensitive"
    monkeypatch.setenv("XAI_API_KEY", secret)
    context.attacker_goal = secret
    logger = ExperimentLogger(tmp_path, context, TAPConfig())
    logger.event("test", echoed=secret)
    for file in logger.directory.iterdir():
        assert secret not in file.read_text()
