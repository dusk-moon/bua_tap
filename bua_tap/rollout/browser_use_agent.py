"""Browser Use adapter. Imports and credentials are required only for real BUA runs."""
import asyncio
import json
import os
import shutil
from pathlib import Path

from ..models import ActionRecord, RolloutResult
from .artifacts import write_artifact
from .errors import RolloutError


def history_result(history, reason: str) -> tuple[RolloutResult, dict]:
    raw = history.model_dump()
    trajectory, traces, actions = [], [], []
    for index, item in enumerate(raw.get("history", []), 1):
        output = item.get("model_output") or {}
        state = item.get("state") or {}
        results = item.get("result") or []
        requested = output.get("action") or []
        trace = {key: output[key] for key in ("thinking", "evaluation_previous_goal", "memory", "next_goal")
                 if output.get(key) is not None}
        traces.append(f"Step {index}: " + json.dumps(trace, ensure_ascii=False))
        trajectory.append({"step": index, "state": state, "state_message": item.get("state_message"),
                           "exposed_trace": trace, "requested_actions": requested,
                           "action_results": results, "metadata": item.get("metadata")})
        # Preserve result ordering without asserting every proposed action was executed.
        for result_index, result in enumerate(results):
            actions.append(ActionRecord(action="browser_use_action_result",
                arguments={"step": index, "result_index": result_index,
                           "requested_action": requested[result_index] if result_index < len(requested) else None},
                result=result))
    usage = getattr(history, "usage", None)
    raw["usage"] = usage.model_dump(mode="json") if usage is not None else None
    result = RolloutResult(reasoning_trace="\n".join(traces), action_log=actions,
        trajectory=trajectory, termination_reason=reason, final_response=history.final_result(),
        agent_reported_success=history.is_successful(),
        metadata={"runner": "real", "framework": "browser-use", "synthetic": False,
                  "agent_done": history.is_done(), "agent_errors": history.errors(), "usage": raw["usage"]})
    return result, raw


class BrowserUseAgent:
    def __init__(self, config, cdp_url, *, agent_factory=None):
        self.config, self.cdp_url = config, cdp_url
        self.agent_factory = agent_factory

    async def run(self, context, directory: Path) -> RolloutResult:
        # Only the benign instruction is supplied to the agent, never the attack goal/history.
        if self.agent_factory:
            agent, browser = self.agent_factory(context.benign_instruction, self.config, self.cdp_url, directory)
        else:
            if not os.environ.get("OPENAI_API_KEY"):
                raise RolloutError("Set OPENAI_API_KEY before running the BUA")
            os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
            from browser_use import Agent, Browser, ChatOpenAI

            browser = Browser(cdp_url=self.cdp_url, keep_alive=True)
            agent = Agent(task=context.benign_instruction, browser=browser,
                llm=ChatOpenAI(model=self.config.model), use_vision=self.config.use_vision,
                max_failures=self.config.max_failures, max_actions_per_step=self.config.max_actions_per_step,
                llm_timeout=self.config.llm_timeout_seconds, step_timeout=self.config.step_timeout_seconds,
                use_judge=False, enable_signal_handler=False, generate_gif=False,
                file_system_path=str(directory / "agent_files"))

        def capture(reason):
            result, raw = history_result(agent.history, reason)
            for index, step in enumerate(raw.get("history", []), 1):
                source = (step.get("state") or {}).get("screenshot_path")
                if source and Path(source).is_file():
                    target = directory / "screenshots" / f"step-{index}{Path(source).suffix}"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if Path(source).resolve() != target.resolve():
                        shutil.copy2(source, target)
                    result.artifacts[f"screenshot_step_{index}"] = str(target.resolve())
            write_artifact(directory / "agent_history.json", raw)
            result.artifacts["agent_history"] = str((directory / "agent_history.json").resolve())
            return result

        async def checkpoint(_agent):
            capture("running")

        failure = None
        reason = "error"
        result = None
        try:
            await asyncio.wait_for(agent.run(max_steps=self.config.max_steps, on_step_end=checkpoint),
                                   timeout=self.config.rollout_timeout_seconds)
            if agent.history.is_done():
                reason = "completed"
            elif len(agent.history.history) >= self.config.max_steps:
                reason = "max_steps"
            else:
                reason = "agent_stopped"
        except asyncio.TimeoutError as exc:
            reason, failure = "timeout", exc
        except BaseException as exc:
            reason, failure = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error", exc
        finally:
            try:
                result = capture(reason)
            finally:
                try:
                    await asyncio.wait_for(browser.stop(), timeout=20)
                except Exception as exc:
                    if failure is None:
                        failure = exc
        if failure is not None:
            if isinstance(failure, (asyncio.CancelledError, KeyboardInterrupt)):
                failure.result = result
                raise failure
            raise RolloutError(f"BUA execution failed ({type(failure).__name__}); partial history saved", result) from None
        if reason == "agent_stopped":
            raise RolloutError("BUA stopped before completion or its step limit", result)
        return result
