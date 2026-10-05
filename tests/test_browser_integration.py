"""Opt-in real Chromium test with a local HTML fixture, no GitLab or LLM access."""
import asyncio
import json
import os
from pathlib import Path
import socket

import pytest

from bua_tap.config import TAPConfig
from bua_tap.rollout.browser import ChromiumSession
from bua_tap.rollout.real import RealBUARolloutRunner


@pytest.mark.skipif(not os.environ.get("TAP_CHROMIUM_PATH"), reason="Set TAP_CHROMIUM_PATH for local browser test")
def test_recorded_gitlab_scripts_in_real_chromium(tmp_path):
    data = json.loads(Path("examples/gitlab_config.json").read_text())
    with socket.socket() as port:
        port.bind(("127.0.0.1", 0))
        debugging_port = port.getsockname()[1]
    data["rollout"]["browser"].update(executable_path=os.environ["TAP_CHROMIUM_PATH"],
                                     user_data_dir=str(tmp_path / "chromium-profile"),
                                     debugging_port=debugging_port)
    config = TAPConfig.model_validate(data).rollout
    # Keep recorded selectors/assertions unchanged; only substitute the destination.
    for name in ("readiness_script", "setup_script", "success_check_script", "cleanup_script"):
        script = getattr(config.surface, name)
        start = script.index('"https://')
        end = script.index('"', start + 1)
        setattr(config.surface, name, script[:start] + '"http://fixture.test/work-item"' + script[end + 1:])
    html = Path("tests/fixtures/work_item.html").read_text()

    class LocalSession(ChromiumSession):
        async def start(self):
            await super().start()
            await self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))

    session = LocalSession(config.browser)
    runner = RealBUARolloutRunner(config, tmp_path / "artifacts", session=session)
    async def exercise():
        try:
            result = await runner.arun('Literal "quotes",\n{braces}, 🧪\n', None, surface_only=True)
            assert result.lifecycle == {
                "setup": "verified",
                "setup_success_check": "verified_clean",
                "bua": "skipped",
                "success_check": "verified",
                "cleanup": "verified",
            }
            page = await session.new_page()
            await page.goto("http://fixture.test/work-item")
            assert await page.locator("#description").inner_text() == ""
            assert result.final_state["pages"][0]["title"] == "Local work item fixture"
            assert result.metadata["surface_setup"]["persisted_attack_string"] == 'Literal "quotes",\n{braces}, 🧪'
        finally:
            await runner.aclose()
        assert session.process is None
    asyncio.run(exercise())
