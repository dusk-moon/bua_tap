"""Validated data contracts shared by search, models, and future runners."""
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class AttackContext(StrictModel):
    attacker_goal: str = Field(min_length=1)
    benign_instruction: str = Field(min_length=1)
    webapp_description: str = Field(min_length=1)
    injection_surface_description: str = Field(min_length=1)


class Message(StrictModel):
    role: Literal["system", "user", "assistant"]
    content: str


class AttackerOutput(StrictModel):
    improvement: str = Field(min_length=1, max_length=1200)
    attack_string: str = Field(min_length=1, max_length=16000)


class ActionRecord(StrictModel):
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    timestamp: str | None = None


class GroundTruthResult(StrictModel):
    success: bool
    summary: str = Field(min_length=1, max_length=1200)
    evidence: dict[str, Any] = Field(default_factory=dict)


class RolloutResult(StrictModel):
    # Only framework-exposed traces; never request hidden chain-of-thought.
    reasoning_trace: str
    action_log: list[ActionRecord]
    final_state: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    trajectory: list[dict[str, Any]] = Field(default_factory=list)
    termination_reason: str | None = None
    agent_reported_success: bool | None = None
    final_response: str | None = None
    ground_truth: GroundTruthResult | None = None
    lifecycle: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)


class EvaluationResult(StrictModel):
    score: float = Field(ge=0, le=10)
    attack_progress_summary: str = Field(min_length=1, max_length=1200)
    benign_task_summary: str = Field(min_length=1, max_length=1200)
    key_evidence: list[Annotated[str, Field(min_length=1, max_length=400)]] = Field(min_length=1, max_length=6)
    limiting_factor: str | None = Field(max_length=1200)
