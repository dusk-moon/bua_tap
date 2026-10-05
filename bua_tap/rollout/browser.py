"""Own one Chromium process; Playwright and Browser Use attach to its local CDP."""
import asyncio
import socket
import subprocess
from pathlib import Path

import httpx

from .config import ChromiumConfig
from .errors import RolloutError


class ChromiumSession:
    def __init__(self, config: ChromiumConfig):
        self.config = config
        self.process = self.playwright = self.browser = self.context = None

    def launch_command(self):
        root, profile = self.config.resolved_profile()
        command = [str(Path(self.config.executable_path).resolve()),
                   f"--remote-debugging-port={self.config.debugging_port}",
                   "--remote-debugging-address=127.0.0.1", f"--user-data-dir={root}",
                   "--no-first-run", "--no-default-browser-check"]
        if profile:
            command.append(f"--profile-directory={profile}")
        if self.config.headless:
            command.append("--headless=new")
        return command + ["about:blank"]

    async def start(self):
        from playwright.async_api import async_playwright

        if not Path(self.config.executable_path).is_file():
            raise RolloutError("Chromium executable_path does not exist")
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", self.config.debugging_port))
            except OSError:
                raise RolloutError("CDP port is already in use; choose a free debugging_port") from None
        try:
            self.process = subprocess.Popen(self.launch_command(), stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = asyncio.get_running_loop().time() + self.config.startup_timeout_seconds
            async with httpx.AsyncClient(trust_env=False) as client:
                while True:
                    if self.process.poll() is not None:
                        raise RolloutError("Chromium exited during startup; close any browser using this profile")
                    try:
                        response = await client.get(self.config.cdp_url + "/json/version", timeout=1)
                        if response.is_success and response.json().get("webSocketDebuggerUrl"):
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    if asyncio.get_running_loop().time() >= deadline:
                        raise RolloutError("Chromium CDP startup timed out; check profile locks and executable")
                    await asyncio.sleep(0.1)
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.connect_over_cdp(
                self.config.cdp_url, timeout=self.config.startup_timeout_seconds * 1000)
            if not self.browser.contexts:
                raise RolloutError("CDP browser has no default authenticated context")
            self.context = self.browser.contexts[0]
            self.context.set_default_timeout(self.config.action_timeout_ms)
            self.context.set_default_navigation_timeout(self.config.navigation_timeout_ms)
        except BaseException:
            await self.close()
            raise

    async def new_page(self):
        return await self.context.new_page()

    async def neutral_page(self):
        # Only use this session's owned browser. Preserve auth, discard previous tabs.
        page = await self.new_page()
        for old_page in list(self.context.pages):
            if old_page != page:
                await old_page.close()
        return page

    async def snapshot(self):
        return {"pages": [{"url": page.url, "title": await page.title()}
                          for page in list(self.context.pages) if not page.is_closed()]}

    async def close(self):
        # Browser.close over CDP closes only this browser, never all chrome.exe processes.
        try:
            if self.browser and self.browser.is_connected():
                try:
                    cdp = await self.browser.new_browser_cdp_session()
                    await asyncio.wait_for(cdp.send("Browser.close"), timeout=5)
                except Exception:
                    pass
        finally:
            try:
                if self.playwright:
                    await self.playwright.stop()
                    self.playwright = None
            finally:
                if self.process:
                    try:
                        await asyncio.to_thread(self.process.wait, timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.terminate()
                        try:
                            await asyncio.to_thread(self.process.wait, timeout=5)
                        except subprocess.TimeoutExpired:
                            self.process.kill()
                            await asyncio.to_thread(self.process.wait)
                    self.process = None
