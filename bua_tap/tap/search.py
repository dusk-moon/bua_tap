from copy import deepcopy
from typing import Callable, Protocol

from ..config import TAPConfig
from ..logging.experiment_logger import ExperimentLogger, utc_now
from ..models import AttackContext, AttackerOutput, EvaluationResult, Message, RolloutResult
from ..rollout.base import BUARolloutRunner
from ..rollout.errors import RolloutError
from .history import feedback_message, initial_history
from .node import AttackNode, SearchResult


class AttackGenerator(Protocol):
    def generate(self, history: list[Message]) -> AttackerOutput: ...


class RolloutEvaluator(Protocol):
    def evaluate(self, context: AttackContext, attack_string: str,
                 rollout: RolloutResult) -> EvaluationResult: ...


def select_top_w(nodes: list[AttackNode], width: int) -> list[AttackNode]:
    if width < 1 or any(node.evaluation is None for node in nodes):
        raise ValueError("Positive width and evaluated nodes required")
    return sorted(nodes, key=lambda n: (-n.evaluation.score, n.generation_order))[:width]


class TAPSearch:
    def __init__(self, config: TAPConfig, attacker: AttackGenerator,
                 evaluator: RolloutEvaluator, runner: BUARolloutRunner, logger: ExperimentLogger,
                 status: Callable[[str], None] | None = None):
        self.config, self.attacker, self.evaluator = config, attacker, evaluator
        self.runner, self.logger = runner, logger
        self.status = status or (lambda _message: None)

    def run(self, context: AttackContext) -> SearchResult:
        root = AttackNode(id="root", parent_id=None, depth=0, branch_index=0,
                          generation_order=0, timestamp=utc_now(), attacker_messages=initial_history(context))
        nodes, frontier = [root], [root]
        self.logger.node(root, "root")
        stage, active_id = "start", "root"
        evaluated_count = 0
        self.status(
            f"Search started: depth={self.config.depth}, branching={self.config.branching_factor}, "
            f"width={self.config.width}."
        )
        try:
            for depth in range(1, self.config.depth + 1):
                children = []
                candidates_at_depth = len(frontier) * self.config.branching_factor
                candidate_at_depth = 0
                self.status(
                    f"Depth {depth}/{self.config.depth} started with {len(frontier)} frontier node(s); "
                    f"{candidates_at_depth} candidate(s) scheduled."
                )
                for parent in frontier:
                    for branch_index in range(self.config.branching_factor):
                        candidate_at_depth += 1
                        active_id = f"n{len(nodes):05d}"
                        stage = "generation"
                        self.logger.event("generation_started", node_id=active_id,
                                          parent_id=parent.id, depth=depth, branch_index=branch_index)
                        self.status(
                            f"Depth {depth}/{self.config.depth}, candidate "
                            f"{candidate_at_depth}/{candidates_at_depth}: {active_id} from {parent.id} "
                            f"(branch {branch_index + 1}/{self.config.branching_factor}); "
                            "waiting for attacker generation."
                        )
                        history = deepcopy(parent.attacker_messages)
                        output = self.attacker.generate(history)
                        self.status(f"{active_id}: attacker candidate generated; starting rollout.")
                        history.append(Message(role="assistant", content=output.model_dump_json()))
                        child = AttackNode(id=active_id, parent_id=parent.id, depth=depth,
                                           branch_index=branch_index, generation_order=len(nodes),
                                           timestamp=utc_now(), attack_string=output.attack_string,
                                           improvement=output.improvement, attacker_messages=history)
                        nodes.append(child)
                        self.logger.node(child, "generated")
                        stage = "rollout"
                        try:
                            child.rollout_result = self.runner.run(child.attack_string, context)
                        except RolloutError as exc:
                            child.rollout_result = exc.result
                            self.logger.node(child, "rollout_failed")
                            raise
                        self.logger.node(child, "rollout_complete")
                        self.status(
                            f"{active_id}: rollout finished with "
                            f"termination={child.rollout_result.termination_reason}, "
                            f"steps={len(child.rollout_result.trajectory)}, "
                            f"ground_truth_success="
                            f"{child.rollout_result.ground_truth.success if child.rollout_result.ground_truth else 'unavailable'}; "
                            "waiting for evaluator."
                        )
                        stage = "evaluation"
                        child.evaluation = self.evaluator.evaluate(context, child.attack_string, child.rollout_result)
                        evaluated_count += 1
                        child.attacker_messages.append(
                            feedback_message(child.evaluation, child.rollout_result.ground_truth)
                        )
                        self.logger.node(child, "evaluated")
                        self.status(
                            f"{active_id}: evaluation complete; score={child.evaluation.score:g}/10, "
                            f"ground_truth_success="
                            f"{child.rollout_result.ground_truth.success if child.rollout_result.ground_truth else 'unavailable'}. "
                            f"Evaluated {evaluated_count} candidate(s)."
                        )
                        children.append(child)
                        if self.config.is_success(child.rollout_result):
                            self.status(f"Browser ground truth confirmed success for {active_id}; stopping search early.")
                            return self._finish(nodes, children, "success", child.id)
                frontier = select_top_w(children, self.config.width)
                self.logger.event("pruning", depth=depth,
                                  candidate_ids=[n.id for n in children],
                                  survivor_ids=[n.id for n in frontier])
                survivor_summary = ", ".join(
                    f"{node.id} ({node.evaluation.score:g})" for node in frontier
                )
                self.status(
                    f"Depth {depth}/{self.config.depth} complete; frontier after pruning: "
                    f"{survivor_summary or 'empty'}."
                )
            self.status("Maximum search depth reached; writing final artifacts and report.")
            return self._finish(nodes, frontier, "max_depth", None)
        except Exception as exc:
            self.status(
                f"Search failed during {stage} for {active_id}: {type(exc).__name__}."
            )
            # Do not serialize arbitrary exception messages: SDKs/runners may echo secrets.
            self.logger.event("run_failed", node_id=active_id, stage=stage, error_type=type(exc).__name__)
            raise

    def _finish(self, nodes, frontier, reason, success_id):
        evaluated = [node for node in nodes if node.evaluation is not None]
        best = select_top_w(evaluated, 1)[0] if evaluated else None
        result = SearchResult(run_id=self.logger.run_id, nodes=nodes, stop_reason=reason,
                              frontier_ids=[node.id for node in frontier], successful_node_id=success_id,
                              best_node_id=best.id if best else None)
        self.logger.finish(result)
        return result
