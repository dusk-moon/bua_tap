from typing import Protocol

from ..models import AttackContext, RolloutResult


class BUARolloutRunner(Protocol):
    def run(self, attack_string: str, context: AttackContext) -> RolloutResult:
        """Reset isolated environment, insert surface text, run benign task, collect trace/actions."""
        ...
