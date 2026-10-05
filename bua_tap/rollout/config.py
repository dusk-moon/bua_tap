"""Configuration for trusted local Playwright scripts and a CDP-connected BUA."""
import textwrap
from pathlib import Path

from pydantic import Field, field_validator

from ..models import StrictModel


def compile_script(source: str, name: str):
    body = textwrap.dedent(source).strip() or "pass"
    return compile("async def __surface_script(page, attack_string, expect):\n" +
                   textwrap.indent(body, "    ") + "\n", f"<rollout:{name}>", "exec")


class ChromiumConfig(StrictModel):
    executable_path: str = Field(min_length=1)
    user_data_dir: str = Field(min_length=1)
    profile_directory: str | None = None
    debugging_port: int = Field(default=9222, ge=1, le=65535)
    headless: bool = False
    startup_timeout_seconds: float = Field(default=30.0, gt=0)
    action_timeout_ms: int = Field(default=15000, gt=0)
    navigation_timeout_ms: int = Field(default=45000, gt=0)

    @field_validator("user_data_dir", "executable_path")
    @classmethod
    def clean_path(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("A nonempty path is required")
        return value

    @field_validator("profile_directory")
    @classmethod
    def profile_name(cls, value):
        if value is not None and (not value.strip() or "/" in value or "\\" in value or value in (".", "..")):
            raise ValueError("profile_directory must be one directory name")
        return value

    @property
    def cdp_url(self):
        return f"http://127.0.0.1:{self.debugging_port}"

    def resolved_profile(self) -> tuple[Path, str | None]:
        supplied = Path(self.user_data_dir).expanduser().resolve()
        # Accept a selected existing profile as well as Chromium's user-data root.
        if (supplied / "Preferences").is_file() and not (supplied / "Local State").exists():
            if (supplied.parent / "Local State").is_file():
                if self.profile_directory is not None and self.profile_directory != supplied.name:
                    raise ValueError("profile_directory conflicts with the supplied profile path")
                return supplied.parent, supplied.name
        return supplied, self.profile_directory


class SurfaceConfig(StrictModel):
    setup_script: str = Field(min_length=1)
    cleanup_script: str = Field(min_length=1)
    readiness_script: str = ""
    success_check_script: str = ""
    script_timeout_seconds: float = Field(default=120.0, gt=0)

    @field_validator("setup_script", "cleanup_script", "readiness_script", "success_check_script")
    @classmethod
    def valid_python(cls, value):
        try:
            compile_script(value, "config")
        except SyntaxError as exc:
            raise ValueError(f"Invalid async Python Playwright script at line {exc.lineno}") from None
        return value


class BrowserUseConfig(StrictModel):
    model: str = Field(default="gpt-4o", min_length=1)
    max_steps: int = Field(default=100, ge=1)
    max_failures: int = Field(default=3, ge=1)
    max_actions_per_step: int = Field(default=5, ge=1)
    use_vision: bool = True
    llm_timeout_seconds: int = Field(default=90, ge=1)
    step_timeout_seconds: int = Field(default=180, ge=1)
    rollout_timeout_seconds: float = Field(default=1200.0, gt=0)


class RealRolloutConfig(StrictModel):
    browser: ChromiumConfig
    surface: SurfaceConfig
    bua: BrowserUseConfig = Field(default_factory=BrowserUseConfig)
