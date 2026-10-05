from pydantic import Field

from ..models import EvaluationResult, Message, RolloutResult, StrictModel


class AttackNode(StrictModel):
    id: str
    parent_id: str | None
    depth: int
    branch_index: int
    generation_order: int
    timestamp: str
    attack_string: str | None = None
    improvement: str | None = None
    rollout_result: RolloutResult | None = None
    evaluation: EvaluationResult | None = None
    attacker_messages: list[Message] = Field(default_factory=list)


class SearchResult(StrictModel):
    run_id: str
    stop_reason: str
    nodes: list[AttackNode]
    frontier_ids: list[str]
    successful_node_id: str | None = None
    best_node_id: str | None = None

    def path(self, node_id: str | None = None) -> list[AttackNode]:
        node_id = node_id or self.successful_node_id or self.best_node_id
        by_id = {node.id: node for node in self.nodes}
        result = []
        while node_id is not None:
            node = by_id[node_id]
            result.append(node)
            node_id = node.parent_id
        return list(reversed(result))
