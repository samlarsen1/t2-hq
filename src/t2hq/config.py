"""Settings, from the environment and then ``.env`` (written by the operator, never by code)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The bot, and the one chat it answers. Messages from any other chat are ignored.
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None
    # Long-poll timeout for getUpdates, in seconds.
    telegram_poll_seconds: int = Field(default=50, ge=1, le=60)

    # Both choices are free: Gemini's free tier, or a local model served by Ollama.
    model_provider: Literal["google", "ollama"] = "google"
    google_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.7-flash"
    gemini_min_interval_seconds: float = Field(default=7.0, ge=0)
    ollama_model: str = "qwen3:4b"
    ollama_base_url: str = "http://localhost:11434/v1"
    # With MODEL_PROVIDER=google, fall back to Ollama when Gemini fails or is rate limited.
    ollama_fallback: bool = False

    # The deployed droids sit side by side with this app.
    apps_dir: Path = Path("..")
    # This app's own state: the SQLite database and dry-run reports.
    state_dir: Path = Path("var")
    # How often to look for new advice and failed runs, in seconds.
    watch_seconds: int = Field(default=60, ge=5)
    dry_run_timeout_seconds: int = Field(default=900, ge=10)

    # Pull requests for accepted suggestions use the gh CLI. Without a token, gh's own login is
    # used; a token here should be limited to the t2-* repos (contents and pull requests).
    github_token: SecretStr | None = None
    gh_path: str = "gh"

    log_level: str = "INFO"
