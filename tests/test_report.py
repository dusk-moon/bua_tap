import json

from bua_tap.llm.fixture import FixtureClient
from bua_tap.models import AttackerOutput, GroundTruthResult, RolloutResult
from bua_tap.rollout.mock import MockBUARolloutRunner


class FinalResponseRunner(MockBUARolloutRunner):
    def run(self, attack_string, context):
        result = super().run(attack_string, context)
        result.final_response = "Readable final BUA response"
        result.termination_reason = "completed"
        result.agent_reported_success = True
        result.lifecycle = {"setup": "verified", "bua": "completed", "cleanup": "verified"}
        return result


class GroundTruthSuccessRunner(FinalResponseRunner):
    def run(self, attack_string, context):
        result = super().run(attack_string, context)
        result.ground_truth = GroundTruthResult(
            success=True, summary="The configured target element exists.", evidence={"visible": True}
        )
        return result


def test_completed_run_writes_human_readable_depth_and_node_report(run_search, context):
    result, _, logger = run_search(runner=FinalResponseRunner(), branching_factor=3, width=2, depth=2)
    report = (logger.directory / "report.md").read_text(encoding="utf-8")
    assert "# TAP run report" in report
    assert context.attacker_goal in report
    assert context.benign_instruction in report
    assert "## Depth 1" in report and "## Depth 2" in report
    assert "Advanced to the next depth: `n00003`, `n00002`." in report
    assert "Pruned at this depth: `n00001`." in report
    for node in result.nodes[1:]:
        assert f"### Node {node.id}" in report
        assert node.attack_string in report
        assert node.improvement in report
    assert "Readable final BUA response" in report
    assert "Attack progress:" in report and "Benign-task behavior:" in report
    assert "not hidden chain-of-thought" in report
    summary = json.loads((logger.directory / "result.json").read_text())
    assert summary["report"] == "report.md"


def test_early_success_report_explains_termination(run_search):
    result, _, logger = run_search(runner=GroundTruthSuccessRunner(),
                                   branching_factor=3, width=2, depth=3)
    report = (logger.directory / "report.md").read_text()
    assert len(result.nodes) == 2
    assert "terminated immediately at `n00001`; later siblings were not generated" in report
    assert "**Successful node:** n00001" in report
    assert "**Browser-verified success:** yes" in report


def test_report_handles_markdown_fences_and_redacts_keys(run_search, monkeypatch):
    secret = "test-report-secret-key"
    monkeypatch.setenv("XAI_API_KEY", secret)

    class FenceClient(FixtureClient):
        def generate(self, messages, schema, **kwargs):
            result = super().generate(messages, schema, **kwargs)
            if schema is AttackerOutput:
                result.attack_string = f"candidate ``` embedded {secret}"
            return result

    _, _, logger = run_search(client=FenceClient(), runner=FinalResponseRunner(),
                              branching_factor=1, width=1, depth=1)
    report = (logger.directory / "report.md").read_text()
    assert secret not in report
    assert "candidate ``` embedded [REDACTED]" in report
    assert "````text" in report
