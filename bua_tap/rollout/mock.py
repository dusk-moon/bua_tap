"""Synthetic plumbing fixture. It measures no real attack effectiveness."""
from ..models import ActionRecord, AttackContext, RolloutResult


class MockBUARolloutRunner:
    def run(self, attack_string: str, context: AttackContext) -> RolloutResult:
        return RolloutResult(
            reasoning_trace="SYNTHETIC FIXTURE: no browser or BUA executed. Simulated page read only.",
            action_log=[ActionRecord(action="fixture_page_read", arguments={"surface_text": attack_string},
                                     result="SYNTHETIC: no actual browser action")],
            final_state={"synthetic": True},
            metadata={"runner": "mock", "synthetic": True, "real_attack_evidence": False},
        )
