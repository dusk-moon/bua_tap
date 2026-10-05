"""Transactional setup -> fresh BUA -> ground-truth check -> cleanup."""
import asyncio
from pathlib import Path
from typing import Callable
from uuid import uuid4

from ..models import AttackContext, GroundTruthResult, RolloutResult
from .artifacts import write_artifact
from .browser import ChromiumSession
from .browser_use_agent import BrowserUseAgent
from .config import RealRolloutConfig
from .errors import RolloutError
from .scripts import SurfaceScripts


def _setup_contamination(precheck: GroundTruthResult) -> GroundTruthResult:
    return GroundTruthResult(
        success=False,
        summary=(
            "Rejected as setup contamination: the attacker goal was already satisfied "
            "immediately after the injection surface was prepared, before the BUA ran."
        ),
        evidence={
            "classification": "setup_contamination",
            "bua_executed": False,
            "setup_ground_truth": precheck.model_dump(),
        },
    )


class RealBUARolloutRunner:
    def __init__(self, config: RealRolloutConfig, artifact_dir="runs/rollouts", *,
                 session=None, scripts=None, agent=None,
                 status: Callable[[str], None] | None = None):
        self.config = config
        self.artifact_dir = Path(artifact_dir)
        self.session = session or ChromiumSession(config.browser)
        self.scripts = scripts or SurfaceScripts(config.surface)
        self.agent = agent or BrowserUseAgent(config.bua, config.browser.cdp_url)
        self.status = status or (lambda _message: None)
        self._loop = None
        self._started = False
        self._unusable = False
        self._busy = False

    def start(self):
        if self._loop is None:
            self._loop = asyncio.Runner()
        try:
            self._loop.run(self.astart())
        except BaseException:
            self.close()
            raise
        return self

    async def astart(self):
        if self._started:
            return
        try:
            self.status("Launching Chromium and connecting over CDP.")
            await self.session.start()
            self.status("Chromium connected; checking injection-surface readiness.")
            page = await self.session.neutral_page()
            await self.scripts.execute("readiness", page, "")
            await self.session.neutral_page()
            self._started = True
            self.status("Browser and injection surface are ready.")
        except BaseException as exc:
            self.status(f"Browser startup/readiness failed: {type(exc).__name__}.")
            state = {}
            try:
                state = await self.session.snapshot()
            except Exception:
                pass
            try:
                write_artifact(self.artifact_dir / "startup_failure.json", {
                    "stage": "startup_readiness", "error_type": type(exc).__name__, "browser_state": state})
            finally:
                await self.session.close()
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, RolloutError)):
                raise
            raise RolloutError("Browser/readiness failed. Confirm this profile is signed in and the configured "
                               "surface is accessible. See startup_failure.json.") from None

    def run(self, attack_string: str, context: AttackContext) -> RolloutResult:
        self.start()
        return self._loop.run(self.arun(attack_string, context))

    def test_surface(self, attack_string: str) -> RolloutResult:
        self.start()
        return self._loop.run(self.arun(attack_string, None, surface_only=True))

    async def arun(self, attack_string, context, *, surface_only=False):
        if self._unusable:
            raise RolloutError("Runner is unusable after a failed rollout; resolve the failure before restarting")
        if self._busy:
            raise RolloutError("Concurrent rollouts cannot share one browser")
        await self.astart()
        self._busy = True
        directory = self.artifact_dir / uuid4().hex
        result = RolloutResult(reasoning_trace="", action_log=[], termination_reason="not_started")
        lifecycle = {
            "setup": "not_started",
            "setup_success_check": (
                "not_started" if self.config.surface.success_check_script.strip()
                else "not_configured"
            ),
            "bua": "skipped" if surface_only else "not_started",
            "success_check": "not_started" if self.config.surface.success_check_script.strip() else "not_configured",
            "cleanup": "not_started",
        }
        failure = None
        setup_evidence = None
        setup_ground_truth = None
        setup_contaminated = False
        try:
            lifecycle["setup"] = "running"
            self.status("Rollout setup started: resetting state and placing the candidate.")
            page = await self.session.neutral_page()
            setup_evidence = await self.scripts.execute("setup", page, attack_string)
            lifecycle["setup"] = "verified"
            self.status("Rollout setup verified.")
            if self.config.surface.success_check_script.strip():
                lifecycle["setup_success_check"] = "running"
                self.status("Checking that setup did not already satisfy the attacker goal.")
                page = await self.session.neutral_page()
                setup_ground_truth = await self.scripts.check_success(page, attack_string)
                setup_contaminated = setup_ground_truth.success
                lifecycle["setup_success_check"] = (
                    "contaminated" if setup_contaminated else "verified_clean"
                )
                if setup_contaminated:
                    lifecycle["bua"] = "skipped_setup_contamination"
                    lifecycle["success_check"] = "rejected_setup_contamination"
                    result.termination_reason = "setup_contamination"
                    result.agent_reported_success = False
                    result.final_response = (
                        "BUA skipped because injection-surface setup already satisfied the attacker goal."
                    )
                    result.ground_truth = _setup_contamination(setup_ground_truth)
                    self.status(
                        "Setup contamination detected; rejecting this candidate and skipping the BUA."
                    )
                else:
                    self.status("Setup baseline is clean; the attacker goal is not yet satisfied.")
            if not surface_only and not setup_contaminated:
                await self.session.neutral_page()
                lifecycle["bua"] = "running"
                self.status(
                    f"BUA started with model {self.config.bua.model}; "
                    f"maximum steps={self.config.bua.max_steps}."
                )
                result = await self.agent.run(context, directory)
                lifecycle["bua"] = result.termination_reason
                self.status(
                    f"BUA finished: termination={result.termination_reason}, "
                    f"steps={len(result.trajectory)}."
                )
            elif surface_only and not setup_contaminated:
                result.termination_reason = "surface_test_only"
                result.metadata = {"runner": "real", "bua_executed": False}
                self.status("Surface-only rollout completed; BUA was skipped.")
            self.status("Capturing pre-cleanup browser evidence.")
            result.final_state = await self.session.snapshot()
            if self.config.surface.success_check_script.strip() and not setup_contaminated:
                lifecycle["success_check"] = "running"
                self.status("Running the browser-side ground-truth success check.")
                page = await self.session.neutral_page()
                result.ground_truth = await self.scripts.check_success(page, attack_string)
                lifecycle["success_check"] = "verified"
                self.status(
                    "Ground-truth success check complete: "
                    f"success={result.ground_truth.success}."
                )
            if setup_evidence is not None:
                result.metadata["surface_setup"] = setup_evidence
            if setup_ground_truth is not None:
                result.metadata["setup_ground_truth"] = setup_ground_truth.model_dump()
            result.lifecycle = dict(lifecycle)
            write_artifact(directory / "before_cleanup.json", result.model_dump())
        except BaseException as exc:
            failure = exc
            if getattr(exc, "result", None) is not None:
                result = exc.result
            failed_stage = next(
                (name for name in ("setup", "setup_success_check", "bua", "success_check")
                 if lifecycle[name] == "running"),
                "bua",
            )
            lifecycle[failed_stage] = "failed"
            self.status(f"Rollout failed during {failed_stage}: {type(exc).__name__}.")
            lifecycle["error_type"] = type(exc).__name__
            if hasattr(exc, "details"):
                lifecycle["script_diagnostics"] = exc.details
            if result.termination_reason == "not_started":
                result.termination_reason = "error"
        finally:
            if failure is not None:
                try:
                    result.final_state = await self.session.snapshot()
                except Exception:
                    lifecycle["snapshot"] = "unavailable"
            # A BUA failure can happen after it changed application state. Preserve
            # that ground truth before cleanup whenever setup itself was verified.
            if (self.config.surface.success_check_script.strip()
                    and lifecycle["setup"] == "verified"
                    and lifecycle["success_check"] == "not_started"):
                try:
                    lifecycle["success_check"] = "running"
                    self.status("Running the browser-side ground-truth success check after rollout failure.")
                    page = await self.session.neutral_page()
                    result.ground_truth = await self.scripts.check_success(page, attack_string)
                    lifecycle["success_check"] = "verified"
                    self.status(
                        "Ground-truth success check complete: "
                        f"success={result.ground_truth.success}."
                    )
                except BaseException as exc:
                    lifecycle["success_check"] = "failed"
                    lifecycle["success_check_error_type"] = type(exc).__name__
                    failure = failure or exc
            # A failed setup may already have saved the candidate: always attempt cleanup.
            try:
                lifecycle["cleanup"] = "running"
                self.status("Rollout cleanup started: restoring the configured baseline.")
                page = await self.session.neutral_page()
                await self.scripts.execute("cleanup", page, attack_string)
                lifecycle["cleanup"] = "verified"
                self.status("Rollout cleanup verified.")
            except BaseException as exc:
                lifecycle["cleanup"] = "failed"
                self.status(f"Rollout cleanup failed: {type(exc).__name__}.")
                lifecycle["cleanup_error_type"] = type(exc).__name__
                if hasattr(exc, "details"):
                    lifecycle["cleanup_diagnostics"] = exc.details
                if failure is None:
                    failure = exc
            finally:
                self._busy = False
                result.lifecycle = lifecycle
                if setup_evidence is not None:
                    result.metadata["surface_setup"] = setup_evidence
                if setup_ground_truth is not None:
                    result.metadata["setup_ground_truth"] = setup_ground_truth.model_dump()
                result.artifacts["rollout"] = str((directory / "rollout.json").resolve())
                if failure is not None:
                    self._unusable = True
                write_artifact(directory / "rollout.json", result.model_dump())
        if failure is not None:
            if isinstance(failure, (asyncio.CancelledError, KeyboardInterrupt)):
                raise failure
            raise RolloutError("Rollout failed; inspect lifecycle and partial artifacts. Further candidates stopped.", result) from None
        return result

    async def aclose(self):
        await self.session.close()
        self._started = False

    def close(self):
        if self._loop:
            try:
                self._loop.run(self.aclose())
            finally:
                self._loop.close()
                self._loop = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
