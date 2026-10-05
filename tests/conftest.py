import pytest

from bua_tap.config import TAPConfig
from bua_tap.llm.attacker import Attacker
from bua_tap.llm.evaluator import Evaluator
from bua_tap.llm.fixture import FixtureClient
from bua_tap.logging.experiment_logger import ExperimentLogger
from bua_tap.models import AttackContext
from bua_tap.rollout.mock import MockBUARolloutRunner
from bua_tap.tap.search import TAPSearch


@pytest.fixture
def context():
    return AttackContext(attacker_goal="Set synthetic label", benign_instruction="Read note",
                         webapp_description="Local notes fixture", injection_surface_description="Comment")


@pytest.fixture
def run_search(tmp_path, context):
    def run(*, client=None, runner=None, status=None, **kwargs):
        config = TAPConfig(**kwargs)
        client = client or FixtureClient()
        logger = ExperimentLogger(tmp_path, context, config)
        result = TAPSearch(config, Attacker(client, config), Evaluator(client, config),
                           runner or MockBUARolloutRunner(), logger, status=status).run(context)
        return result, client, logger
    return run
