from copy import deepcopy

from bua_tap.llm.fixture import FixtureClient
from bua_tap.models import AttackerOutput


class RecordingClient(FixtureClient):
    def __init__(self):
        super().__init__()
        self.inputs = []

    def generate(self, messages, schema, **kwargs):
        if schema is AttackerOutput:
            self.inputs.append(deepcopy(messages))
        return super().generate(messages, schema, **kwargs)


def test_three_separate_generations_from_identical_parent(run_search):
    client = RecordingClient()
    result, _, _ = run_search(client=client, branching_factor=3, width=2, depth=1)
    children = result.nodes[1:]
    assert client.attacker_calls == client.evaluator_calls == len(children) == 3
    assert client.inputs[0] == client.inputs[1] == client.inputs[2] == result.nodes[0].attacker_messages
    assert {n.parent_id for n in children} == {"root"}
    assert [n.branch_index for n in children] == [0, 1, 2]


def test_all_surviving_parents_expand_independently(run_search):
    result, client, _ = run_search(branching_factor=3, width=2, depth=3)
    assert client.attacker_calls == client.evaluator_calls == 3 + 6 + 6
    assert max(n.depth for n in result.nodes) == 3
    assert result.stop_reason == "max_depth"


def test_search_status_describes_generation_rollout_evaluation_and_pruning(run_search):
    messages = []
    run_search(branching_factor=1, width=1, depth=1, status=messages.append)
    text = "\n".join(messages)
    assert "Depth 1/1 started" in text
    assert "n00001 from root" in text
    assert "waiting for attacker generation" in text
    assert "starting rollout" in text
    assert "waiting for evaluator" in text
    assert "evaluation complete" in text
    assert "frontier after pruning" in text
    assert "Maximum search depth reached" in text
