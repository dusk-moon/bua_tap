"""Execute trusted config code. Candidate text is an argument, never source code."""
import asyncio

from ..models import GroundTruthResult
from .config import SurfaceConfig, compile_script
from .errors import RolloutError


class SurfaceScriptError(RolloutError):
    def __init__(self, name, cause):
        super().__init__(f"Surface {name} failed ({type(cause).__name__})")
        # Playwright diagnostics contain page/payload evidence, not API request headers.
        # Persisted through the normal credential-redacting artifact writer.
        self.details = str(cause)


class SurfaceScripts:
    def __init__(self, config: SurfaceConfig):
        self.config = config
        self.functions = {}
        for name in ("readiness", "setup", "success_check", "cleanup"):
            namespace = {}
            exec(compile_script(getattr(config, name + "_script"), name), namespace)
            self.functions[name] = namespace["__surface_script"]

    async def execute(self, name, page, attack_string):
        from playwright.async_api import expect

        try:
            return await asyncio.wait_for(self.functions[name](page, attack_string, expect),
                                          timeout=self.config.script_timeout_seconds)
        except TimeoutError:
            raise
        except Exception as exc:
            raise SurfaceScriptError(name, exc) from None

    async def check_success(self, page, attack_string) -> GroundTruthResult | None:
        if not self.config.success_check_script.strip():
            return None
        raw = await self.execute("success_check", page, attack_string)
        if isinstance(raw, bool):
            return GroundTruthResult(
                success=raw,
                summary="Configured browser-side success check returned true." if raw else
                        "Configured browser-side success check returned false.",
            )
        try:
            return GroundTruthResult.model_validate(raw)
        except Exception as exc:
            raise SurfaceScriptError("success_check", exc) from None
