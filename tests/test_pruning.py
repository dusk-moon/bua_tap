import json

from bua_tap.tap.search import select_top_w


def test_top_width_and_generation_order_ties(run_search):
    result, _, _ = run_search(branching_factor=3, width=2, depth=1)
    children = result.nodes[1:]
    assert [n.evaluation.score for n in select_top_w(children, 2)] == [9, 6]
    for node in children:
        node.evaluation.score = 5.0
        node.attack_string = "identical sibling text"
    assert select_top_w(list(reversed(children)), 2) == children[:2]


def test_pruning_logged_and_only_survivors_expanded(run_search):
    result, _, logger = run_search(branching_factor=3, width=2, depth=2)
    records = [json.loads(line) for line in (logger.directory / "events.jsonl").read_text().splitlines()]
    first = next(r for r in records if r["event"] == "pruning")
    assert len(first["survivor_ids"]) == 2
    assert {n.parent_id for n in result.nodes if n.depth == 2} == set(first["survivor_ids"])
