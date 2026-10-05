import argparse
import json
import os
import time
from importlib.util import find_spec
from pathlib import Path

from pydantic import ValidationError

from .config import TAPConfig
from .llm.attacker import Attacker
from .llm.evaluator import Evaluator
from .llm.fixture import FixtureClient
from .llm.grok_client import GrokClient, LLMError
from .logging.experiment_logger import ExperimentLogger
from .models import AttackContext
from .rollout.mock import MockBUARolloutRunner
from .rollout.real import RealBUARolloutRunner
from .rollout.errors import RolloutError
from .tap.search import TAPSearch


class CLIStatus:
    def __init__(self):
        self.started = time.monotonic()

    def __call__(self, message):
        elapsed = int(time.monotonic() - self.started)
        minutes, seconds = divmod(elapsed, 60)
        print(f"[TAP +{minutes:02d}:{seconds:02d}] {message}", flush=True)


def parser():
    p = argparse.ArgumentParser(description="Controlled BUA TAP research prototype")
    p.add_argument("--context", type=Path)
    p.add_argument("--config", type=Path)
    for flag in ("attacker-goal", "benign-instruction", "webapp-description", "surface-description"):
        p.add_argument("--" + flag)
    for flag in ("branching-factor", "width", "depth", "max-retries", "attacker-max-tokens", "evaluator-max-tokens"):
        p.add_argument("--" + flag, type=int)
    for flag in ("attacker-temperature", "evaluator-temperature", "timeout-seconds"):
        p.add_argument("--" + flag, type=float)
    for flag in ("attacker-model", "evaluator-model", "success-criterion"):
        p.add_argument("--" + flag)
    p.add_argument("--rollout-runner", choices=("mock", "real"), default="mock")
    p.add_argument("--llm-backend", choices=("grok", "fixture"), default="grok",
                   help="fixture is offline scripted data; no attack measurements")
    p.add_argument("--output-dir", type=Path, default=Path("runs"))
    tests = p.add_mutually_exclusive_group()
    tests.add_argument("--surface-test", action="store_true", help="Run real setup/cleanup only; no LLMs or BUA")
    tests.add_argument("--rollout-test", action="store_true", help="Run one real BUA rollout; no attacker/evaluator calls")
    p.add_argument("--attack-file", type=Path, help="Literal UTF-8 payload for --surface-test or --rollout-test")
    p.add_argument("--env-file", type=Path, help="Explicitly load API credentials from a dotenv file without logging them")
    return p


def load_inputs(args):
    context_data = json.loads(args.context.read_text(encoding="utf-8-sig")) if args.context else {}
    for name in ("attacker_goal", "benign_instruction", "webapp_description", "surface_description"):
        value = getattr(args, name)
        if value is not None:
            context_data["injection_surface_description" if name == "surface_description" else name] = value
    config_data = {}
    if os.environ.get("XAI_MODEL"):
        config_data.update(attacker_model=os.environ["XAI_MODEL"], evaluator_model=os.environ["XAI_MODEL"])
    if args.config:
        config_data.update(json.loads(args.config.read_text(encoding="utf-8-sig")))
    for name in TAPConfig.model_fields:
        value = getattr(args, name, None)
        if value is not None:
            config_data[name] = value
    return AttackContext.model_validate(context_data), TAPConfig.model_validate(config_data)


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    client = None
    runner = None
    logger = None
    status = CLIStatus()
    try:
        if args.env_file:
            from dotenv import load_dotenv
            if not args.env_file.is_file():
                raise RolloutError("The supplied --env-file does not exist")
            load_dotenv(args.env_file, override=False)
        context, config = load_inputs(args)
        standalone_test = args.surface_test or args.rollout_test
        if standalone_test and args.rollout_runner != "real":
            raise RolloutError("Standalone rollout tests require --rollout-runner real")
        if args.attack_file and not standalone_test:
            raise RolloutError("--attack-file is only used with --surface-test or --rollout-test")
        if args.rollout_runner == "real":
            if config.rollout is None:
                raise RolloutError("Real rollouts require a rollout object in the config file")
            if find_spec("playwright") is None:
                raise RolloutError('Install rollout dependencies: pip install -e ".[rollout]"')
            if not args.surface_test:
                if find_spec("browser_use") is None:
                    raise RolloutError('Install rollout dependencies: pip install -e ".[rollout]"')
                if not os.environ.get("OPENAI_API_KEY"):
                    raise RolloutError("Set OPENAI_API_KEY before running the BUA, or use --surface-test")
        if not standalone_test:
            client = FixtureClient() if args.llm_backend == "fixture" else GrokClient(config)
        logger = ExperimentLogger(args.output_dir, context, config, metadata={
            "llm_backend": "none" if standalone_test else args.llm_backend,
            "rollout_runner": args.rollout_runner, "surface_test": args.surface_test,
            "rollout_test": args.rollout_test,
            "synthetic": args.rollout_runner == "mock" or (not standalone_test and args.llm_backend == "fixture"),
        })
        print(f"Run directory: {logger.directory.resolve()}", flush=True)
        if args.rollout_runner == "real":
            runner = RealBUARolloutRunner(config.rollout, logger.directory / "rollouts")
            runner.status = status
            root, profile = config.rollout.browser.resolved_profile()
            print(f"Chromium user-data root: {root}; profile: {profile or 'Default'}")
            # Startup/readiness happen before generating any candidates.
            runner.start()
        else:
            runner = MockBUARolloutRunner()
            print("SYNTHETIC MOCK ROLLOUT: this run does not measure real attack effectiveness.")
        if args.surface_test:
            payload = args.attack_file.read_text(encoding="utf-8") if args.attack_file else "TAP surface setup/cleanup test"
            result = runner.test_surface(payload)
            logger.event("surface_test_finished", result=result.model_dump())
            print("Surface setup and cleanup verified; no BUA or LLM ran.")
            return 0
        if args.rollout_test:
            payload = args.attack_file.read_text(encoding="utf-8") if args.attack_file else "TAP rollout integration test. Summarize this work item normally."
            result = runner.run(payload, context)
            logger.event("rollout_test_finished", result=result.model_dump())
            print(f"BUA stopped: {result.termination_reason}; steps={len(result.trajectory)}; "
                  f"cleanup={result.lifecycle.get('cleanup')}")
            print(f"Rollout artifact: {result.artifacts.get('rollout')}")
            return 0
        result = TAPSearch(config, Attacker(client, config), Evaluator(client, config),
                           runner, logger, status=status).run(context)
        for node in result.nodes:
            if node.evaluation:
                print(f"{'  ' * node.depth}{node.id} <- {node.parent_id} "
                      f"score={node.evaluation.score:g} "
                      f"ground_truth_success="
                      f"{node.rollout_result.ground_truth.success if node.rollout_result and node.rollout_result.ground_truth else 'unavailable'}")
        print(f"Stopped: {result.stop_reason}; evaluated={len(result.nodes)-1}; best={result.best_node_id}")
        print("Path: " + " -> ".join(n.id for n in result.path()))
        return 0
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors(include_input=False))
        p.exit(2, f"Invalid context/config fields: {fields}\n")
    except (LLMError, RolloutError) as exc:
        if logger:
            logger.event("cli_failed", error_type=type(exc).__name__,
                         partial_rollout=exc.result.model_dump() if isinstance(exc, RolloutError) and exc.result else None)
        p.exit(2, f"{exc}\n")
    except (OSError, ValueError, TypeError):
        p.exit(2, "Could not read/write experiment files or parse JSON inputs.\n")
    finally:
        try:
            if isinstance(runner, RealBUARolloutRunner):
                runner.close()
        finally:
            if isinstance(client, GrokClient):
                client.close()


if __name__ == "__main__":
    raise SystemExit(main())
