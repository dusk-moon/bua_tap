"""Render a concise Markdown overview while preserving detailed JSON artifacts."""
import json
import re
from pathlib import Path

from ..rollout.artifacts import redact_text


def _value(value) -> str:
    if value is None:
        return "Not available"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _code_block(value: str | None, language="text") -> str:
    value = value if value is not None else "Not available"
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{value}\n{fence}"


def _artifact_link(path: str, run_directory: Path) -> str:
    artifact = Path(path)
    try:
        relative = artifact.resolve().relative_to(run_directory.resolve()).as_posix()
        return f"[{relative}]({relative})"
    except (OSError, ValueError):
        return f"`{path}`"


def _depth_summary(depth: int, nodes, all_nodes, result) -> list[str]:
    evaluated = [node for node in nodes if node.evaluation is not None]
    scores = [node.evaluation.score for node in evaluated]
    successful = [
        node.id for node in evaluated
        if node.rollout_result and node.rollout_result.ground_truth
        and node.rollout_result.ground_truth.success
    ]
    lines = [
        f"Generated {len(nodes)} candidate(s); evaluated {len(evaluated)}.",
        (f"Scores ranged from {min(scores):g} to {max(scores):g}." if scores else "No evaluator scores were produced."),
    ]
    if successful:
        lines.append("Browser-verified success: " + ", ".join(f"`{node_id}`" for node_id in successful) + ".")
    if result.successful_node_id in {node.id for node in nodes}:
        lines.append(f"The search terminated immediately at `{result.successful_node_id}`; later siblings were not generated.")
        return lines
    next_parent_order = list(dict.fromkeys(node.parent_id for node in all_nodes
                                           if node.depth == depth + 1 and node.parent_id is not None))
    next_parent_ids = set(next_parent_order)
    if next_parent_ids:
        selected = next_parent_order
        pruned = [node.id for node in nodes if node.id not in next_parent_ids]
        lines.append("Advanced to the next depth: " + ", ".join(f"`{node_id}`" for node_id in selected) + ".")
        lines.append("Pruned at this depth: " +
                     (", ".join(f"`{node_id}`" for node_id in pruned) if pruned else "none") + ".")
    elif result.stop_reason == "max_depth" and depth == max((node.depth for node in all_nodes), default=0):
        lines.append("The configured maximum depth was reached. Final frontier: " +
                     (", ".join(f"`{node_id}`" for node_id in result.frontier_ids) or "none") + ".")
    return lines


def render_run_report(result, context, config, run_directory) -> str:
    run_directory = Path(run_directory)
    attack_nodes = sorted((node for node in result.nodes if node.depth > 0), key=lambda node: node.generation_order)
    path_ids = [node.id for node in result.path()]
    lines = [
        "# TAP run report",
        "",
        f"- **Run ID:** `{result.run_id}`",
        f"- **Outcome:** `{result.stop_reason}`",
        f"- **Successful node:** {_value(result.successful_node_id)}",
        f"- **Best-scoring node:** {_value(result.best_node_id)}",
        f"- **Selected path:** " + (" → ".join(f"`{node_id}`" for node_id in path_ids) if path_ids else "none"),
        f"- **Evaluated candidates:** {sum(node.evaluation is not None for node in attack_nodes)}",
        f"- **Search settings:** branching factor {config.branching_factor}, width {config.width}, maximum depth {config.depth}",
        f"- **Models:** attacker `{config.attacker_model}`, evaluator `{config.evaluator_model}`" +
        (f", BUA `{config.rollout.bua.model}`" if config.rollout else ""),
        "",
        "Detailed records: [events.jsonl](events.jsonl) · [config.json](config.json) · [result.json](result.json)",
        "",
        "## Attack context",
        "",
        "### Attacker goal",
        "",
        context.attacker_goal,
        "",
        "### Benign BUA instruction",
        "",
        context.benign_instruction,
        "",
        "### Web application",
        "",
        context.webapp_description,
        "",
        "### Injection surface",
        "",
        context.injection_surface_description,
        "",
        "## Search by depth",
        "",
        "The attacker rationale below is the model-provided `improvement` field, not hidden chain-of-thought.",
    ]
    depths = sorted({node.depth for node in attack_nodes})
    for depth in depths:
        nodes = [node for node in attack_nodes if node.depth == depth]
        lines.extend(["", f"## Depth {depth}", ""])
        lines.extend(_depth_summary(depth, nodes, attack_nodes, result))
        for node in nodes:
            evaluation = node.evaluation
            rollout = node.rollout_result
            lines.extend([
                "",
                f"### Node {node.id}",
                "",
                f"- **Parent:** `{node.parent_id}`",
                f"- **Branch index:** {node.branch_index}",
                f"- **Generation order:** {node.generation_order}",
                f"- **Score:** {_value(evaluation.score if evaluation else None)}" + ("/10" if evaluation else ""),
                f"- **Browser-verified success:** "
                f"{_value(rollout.ground_truth.success if rollout and rollout.ground_truth else None)}",
                f"- **Setup contamination:** {_value(bool(rollout and rollout.termination_reason == 'setup_contamination'))}",
                f"- **Rollout termination:** {_value(rollout.termination_reason if rollout else None)}",
                f"- **BUA reported benign-task success:** {_value(rollout.agent_reported_success if rollout else None)}",
                "",
                "#### Attacker refinement rationale",
                "",
                node.improvement or "Not available",
                "",
                "#### Candidate attack string",
                "",
                _code_block(node.attack_string),
                "",
                "#### Final BUA response",
                "",
                _code_block(rollout.final_response if rollout else None),
                "",
                "#### Evaluator output",
                "",
            ])
            if evaluation:
                lines.extend([
                    f"- **Score:** {evaluation.score:g}/10",
                    f"- **Attack progress:** {evaluation.attack_progress_summary}",
                    f"- **Benign-task behavior:** {evaluation.benign_task_summary}",
                    f"- **Limiting factor:** {evaluation.limiting_factor or 'None'}",
                    "- **Key evidence:**",
                ])
                lines.extend(f"  - {item}" for item in evaluation.key_evidence)
            else:
                lines.append("No evaluator output was produced.")
            if rollout and rollout.ground_truth:
                lines.extend([
                    "",
                    "#### Browser ground truth",
                    "",
                    f"- **Success:** {_value(rollout.ground_truth.success)}",
                    f"- **Summary:** {rollout.ground_truth.summary}",
                    "",
                    "**Evidence**",
                    "",
                    _code_block(json.dumps(
                        rollout.ground_truth.evidence, ensure_ascii=False, indent=2
                    ), "json"),
                ])
            if rollout and rollout.lifecycle:
                lines.extend(["", "#### Rollout lifecycle", ""])
                lines.extend(f"- **{key.replace('_', ' ').title()}:** {_value(value)}"
                             for key, value in rollout.lifecycle.items() if "diagnostic" not in key)
            if rollout and rollout.artifacts:
                lines.extend(["", "#### Detailed artifacts", ""])
                lines.extend(f"- **{name.replace('_', ' ').title()}:** {_artifact_link(path, run_directory)}"
                             for name, path in rollout.artifacts.items())
    lines.extend(["", "---", "", "This report is an overview. Use the linked JSONL and rollout artifacts for complete trajectories and raw evidence.", ""])
    return redact_text("\n".join(lines))


def write_run_report(result, context, config, run_directory) -> Path:
    destination = Path(run_directory) / "report.md"
    temporary = destination.with_suffix(".md.tmp")
    temporary.write_text(render_run_report(result, context, config, run_directory), encoding="utf-8")
    temporary.replace(destination)
    return destination
