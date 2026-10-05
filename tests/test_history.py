from bua_tap.models import Message


def test_history_isolation_and_root(run_search):
    result, _, _ = run_search(branching_factor=3, depth=1)
    root, one, two, _ = result.nodes
    assert root.attack_string is root.rollout_result is root.evaluation is None
    assert len(root.attacker_messages) == 2
    one.attacker_messages[0].content = "mutated"
    one.attacker_messages.append(Message(role="user", content="private"))
    assert root.attacker_messages[0].content != "mutated"
    assert two.attacker_messages[0].content != "mutated"
    assert len(two.attacker_messages) == 4


def test_feedback_and_previous_improvement_propagate_without_raw_trace(run_search):
    result, _, _ = run_search(branching_factor=1, depth=2)
    root, parent, child = result.nodes
    assert child.attacker_messages[:4] == parent.attacker_messages
    assert parent.improvement in child.attacker_messages[2].content
    assert parent.evaluation.attack_progress_summary in child.attacker_messages[3].content
    assert [m.role for m in child.attacker_messages] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert all(parent.rollout_result.reasoning_trace not in m.content for m in child.attacker_messages)
