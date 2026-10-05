import pytest

from bua_tap import cli
from bua_tap.models import RolloutResult


def test_surface_cli_does_not_construct_llm_clients(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    events = []
    class Runner:
        def __init__(self, *args): pass
        def start(self): events.append("start")
        def test_surface(self, payload):
            events.append(payload)
            return RolloutResult(reasoning_trace="", action_log=[], termination_reason="surface_test_only")
        def close(self): events.append("close")
    def forbidden(*args): raise AssertionError("LLM clients must not be constructed")
    monkeypatch.setattr(cli, "RealBUARolloutRunner", Runner)
    monkeypatch.setattr(cli, "FixtureClient", forbidden)
    # Leave GrokClient as a class for the CLI's isinstance cleanup check.
    monkeypatch.setattr(cli.GrokClient, "__init__", forbidden)
    assert cli.main(["--context", "examples/gitlab_context.json", "--config", "examples/gitlab_config.json",
                     "--rollout-runner", "real", "--surface-test", "--output-dir", str(tmp_path)]) == 0
    assert events == ["start", "TAP surface setup/cleanup test", "close"]
    assert "no BUA or LLM ran" in capsys.readouterr().out


def test_full_real_run_requires_openai_before_launch(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit) as error:
        cli.main(["--context", "examples/gitlab_context.json", "--config", "examples/gitlab_config.json",
                  "--rollout-runner", "real", "--output-dir", str(tmp_path)])
    assert error.value.code == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_rollout_test_loads_env_without_grok_or_key_logging(tmp_path, monkeypatch, capsys):
    import os
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    key_file = tmp_path / ".env"
    key_file.write_text("OPENAI_API_KEY=test-private-key\n")
    events = []
    class Runner:
        def __init__(self, *args): pass
        def start(self): events.append("start")
        def run(self, payload, context):
            assert os.environ["OPENAI_API_KEY"] == "test-private-key"
            events.append("bua")
            return RolloutResult(reasoning_trace="", action_log=[], termination_reason="completed",
                                 lifecycle={"cleanup": "verified"})
        def close(self): events.append("close")
    def forbidden(*args): raise AssertionError("No TAP LLM client allowed")
    monkeypatch.setattr(cli, "RealBUARolloutRunner", Runner)
    monkeypatch.setattr(cli.GrokClient, "__init__", forbidden)
    output = tmp_path / "runs"
    assert cli.main(["--context", "examples/gitlab_context.json", "--config", "examples/gitlab_config.json",
                     "--rollout-runner", "real", "--rollout-test", "--env-file", str(key_file),
                     "--output-dir", str(output)]) == 0
    assert events == ["start", "bua", "close"]
    assert "test-private-key" not in capsys.readouterr().out
    for file in output.rglob("*.json*"):
        assert "test-private-key" not in file.read_text()
