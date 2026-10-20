import os
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_codex_workdir() -> str:
    """Return the platform-appropriate default Codex working directory.

    On Windows a root-relative path such as ``/workspace`` resolves against the
    current drive (e.g. ``E:/workspace``), so default to a per-user location
    instead. POSIX keeps ``/workspace`` to match the Docker image layout.
    """

    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return str(Path(base) / "codex-openai-bridge" / "workspace")
    return "/workspace"


class Settings(BaseSettings):
    """Application settings loaded from environment variables and `.env`."""

    proxy_api_key: Optional[str] = Field(default=None, alias="PROXY_API_KEY")
    rate_limit_per_minute: int = Field(default=0, alias="RATE_LIMIT_PER_MINUTE")
    codex_path: str = Field(default="codex", alias="CODEX_PATH")
    codex_workdir: str = Field(default_factory=_default_codex_workdir, alias="CODEX_WORKDIR")
    sandbox_mode: str = Field(default="read-only", alias="CODEX_SANDBOX_MODE")
    timeout_seconds: int = Field(default=120, alias="CODEX_TIMEOUT")
    max_parallel_requests: int = Field(default=8, alias="CODEX_MAX_PARALLEL_REQUESTS")
    # Pre-started threads kept per system prompt, so requests skip thread start and
    # Codex's per-thread setup before the first turn (~0.6-1 s).
    # Default matches max_parallel_requests so a full burst finds spares.
    idle_threads: int = Field(default=8, alias="CODEX_IDLE_THREADS")
    # Start the app-server with plugins, MCP servers, hooks, tools and injected
    # agent instructions turned off.
    lean: bool = Field(default=True, alias="CODEX_LEAN")

    # `.env` may also hold variables for docker compose (HOST_UID, ...); ignore them.
    model_config = SettingsConfigDict(case_sensitive=False, env_file=".env", extra="ignore")


def codex_home() -> Path:
    """The Codex home directory (where auth.json, config.toml and AGENTS.md live)."""

    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


# Allow overriding the env file path via the process env `CODEX_ENV_FILE`.
settings = Settings(_env_file=os.getenv("CODEX_ENV_FILE") or ".env")
