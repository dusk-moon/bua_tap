import pytest
from pydantic import ValidationError

from bua_tap.cli import load_inputs, parser
from bua_tap.config import TAPConfig


@pytest.mark.parametrize("values", [{"depth": 0}, {"width": -1}, {"branching_factor": True},
    {"depth": "3"}, {"timeout_seconds": float("inf")}, {"unexpected": 1}])
def test_invalid_configuration(values):
    with pytest.raises(ValidationError):
        TAPConfig(**values)


def test_config_precedence(monkeypatch):
    monkeypatch.setenv("XAI_MODEL", "environment-model")
    args = parser().parse_args(["--context", "examples/example_context.json", "--config",
                               "examples/default_config.json", "--width", "4", "--attacker-model", "cli-model"])
    _, config = load_inputs(args)
    assert config.attacker_model == "cli-model"
    assert config.evaluator_model == "grok-4.6"
    assert config.width == 4
    args = parser().parse_args(["--context", "examples/example_context.json"])
    assert load_inputs(args)[1].attacker_model == "environment-model"
