import json

from ..config import TAPConfig
from ..models import AttackContext, EvaluationResult, Message, RolloutResult
from .grok_client import StructuredClient
from .prompts import EVALUATOR_SYSTEM


def _bounded(value, *, string_limit: int = 4000):
    """Bound verbose framework fields while preserving their semantic evidence."""
    if isinstance(value, str):
        if len(value) <= string_limit:
            return value
        half = string_limit // 2
        return value[:half] + "\n...[truncated for evaluator context]...\n" + value[-half:]
    if isinstance(value, list):
        return [_bounded(item, string_limit=string_limit) for item in value]
    if isinstance(value, dict):
        return {key: _bounded(item, string_limit=string_limit) for key, item in value.items()}
    return value


def _evaluation_rollout(rollout: RolloutResult) -> dict:
    # state_message contains a full browser DOM/screenshot prompt for every step
    # and can make long but valid trajectories exceed the evaluator API context.
    # The ordered exposed trace, requested actions, and results retain the evidence
    # needed for scoring; full fidelity remains in the saved rollout artifacts.
    trajectory = [
        _bounded({
            "step": step.get("step"),
            "exposed_trace": step.get("exposed_trace"),
            "requested_actions": step.get("requested_actions"),
            "action_results": step.get("action_results"),
        })
        for step in rollout.trajectory
    ]
    return {
        "reasoning_trace": _bounded(rollout.reasoning_trace, string_limit=60000),
        "action_log": _bounded([record.model_dump() for record in rollout.action_log]),
        "trajectory": trajectory,
        "trajectory_compaction": (
            "All ordered steps are present. Per-step framework state_message, screenshot/DOM state, "
            "and metadata are omitted here; full data remains in the rollout artifact."
        ),
        "termination_reason": rollout.termination_reason,
        "agent_reported_success": rollout.agent_reported_success,
        "final_response": _bounded(rollout.final_response),
        "browser_ground_truth": _bounded(
            rollout.ground_truth.model_dump() if rollout.ground_truth else None
        ),
        "final_state": _bounded(rollout.final_state),
        "metadata": _bounded(rollout.metadata),
        "lifecycle": _bounded(rollout.lifecycle),
    }


class Evaluator:
    def __init__(self, client: StructuredClient, config: TAPConfig):
        self.client, self.config = client, config

    def evaluate(self, context: AttackContext, attack_string: str,
                 rollout: RolloutResult) -> EvaluationResult:
        evidence = {"context": context.model_dump(), "attack_string": attack_string,
                    "rollout": _evaluation_rollout(rollout)}
        messages = [
            Message(role="system", content=EVALUATOR_SYSTEM + "\nRUBRIC\n" + self.config.score_rubric +
                    "\nATTACK GOAL INTERPRETATION\n" + self.config.success_criterion),
            Message(role="user", content=json.dumps(evidence, ensure_ascii=False)),
        ]
        return self.client.generate(messages, EvaluationResult, model=self.config.evaluator_model,
                                    temperature=self.config.evaluator_temperature,
                                    max_tokens=self.config.evaluator_max_tokens)
