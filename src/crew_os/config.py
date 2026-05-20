"""Application configuration with security gates.

All settings come from environment variables (prefix ``CREW_``) or the
``.env`` file in the project root. The most important invariants enforced
here:

* Web dashboard binds to loopback only.
* Ollama host points to a loopback address only.
* LAB_MODE activation requires a fresh consent token with strict perms.

Tests should instantiate :class:`Settings` directly; production callers
should use :func:`get_settings` (cached).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATA_DIR: Path = Path.home() / ".local" / "share" / "textual-crew-os"
LAB_TOKEN_PATH: Path = Path.home() / ".crew" / "lab_consent.token"
LAB_TOKEN_MAX_AGE: timedelta = timedelta(days=7)

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    """Top-level settings object. Validated at construction time."""

    model_config = SettingsConfigDict(
        env_prefix="CREW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Paths ─────────────────────────────────────────────────────────
    data_dir: Path = Field(default=DEFAULT_DATA_DIR)

    # ── Ollama ────────────────────────────────────────────────────────
    ollama_host: str = Field(default="http://127.0.0.1:11434")
    ollama_timeout: int = Field(default=120, ge=10, le=600)

    # ── Anthropic (optional - offline-first) ─────────────────────────
    anthropic_api_key: SecretStr | None = Field(default=None)
    anthropic_model: str = Field(default="claude-opus-4-7")

    # ── Web dashboard ────────────────────────────────────────────────
    web_host: str = Field(default="127.0.0.1")
    web_port: int = Field(default=8765, ge=1024, le=65535)

    # ── Logging ──────────────────────────────────────────────────────
    log_level: LogLevel = Field(default="INFO")

    # ── Auditor (garak isolated venv) ────────────────────────────────
    garak_python: Path | None = Field(default=None)

    # ── LAB_MODE (dangerous) ─────────────────────────────────────────
    lab_mode: bool = Field(default=False)

    # ── Validators ───────────────────────────────────────────────────
    @field_validator("web_host")
    @classmethod
    def _web_host_must_be_loopback(cls, v: str) -> str:
        if v.lower() not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"web_host must be one of {sorted(_LOOPBACK_HOSTS)}; got {v!r}. "
                "Binding the dashboard to a non-loopback interface exposes "
                "agent control to the network."
            )
        return v

    @field_validator("ollama_host")
    @classmethod
    def _ollama_host_must_be_loopback(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"ollama_host must be a URL like 'http://127.0.0.1:11434'; got {v!r}")
        if parsed.hostname.lower() not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"ollama_host must point to loopback, got hostname {parsed.hostname!r}. "
                "An Ollama exposed beyond the host lets anyone on the network use "
                "your GPU and the models you have loaded."
            )
        return v

    @model_validator(mode="after")
    def _validate_lab_mode(self) -> Settings:
        if self.lab_mode:
            try:
                _verify_lab_consent()
            except ValueError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                raise ValueError(str(exc)) from exc
        return self

    # ── Helpers ──────────────────────────────────────────────────────
    def ensure_dirs(self) -> None:
        """Create the data directory layout if missing."""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "audit").mkdir(exist_ok=True)
        (self.data_dir / "traces").mkdir(exist_ok=True)
        (self.data_dir / "memory").mkdir(exist_ok=True)


def _verify_lab_consent() -> None:
    """Validate the LAB_MODE consent token.

    Raises:
        ValueError: if the token is missing, world-readable, malformed,
            or older than :data:`LAB_TOKEN_MAX_AGE`.
    """

    token_path = LAB_TOKEN_PATH
    if not token_path.exists():
        raise ValueError(
            f"LAB_MODE requested but consent token not found at {token_path}. "
            "Create it explicitly, e.g.:\n"
            f"  mkdir -p {token_path.parent} && "
            f"date -u +%s > {token_path} && chmod 600 {token_path}"
        )

    mode = token_path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ValueError(
            f"LAB consent token at {token_path} has loose permissions "
            f"({oct(mode)}). Run: chmod 600 {token_path}"
        )

    try:
        ts_text = token_path.read_text(encoding="utf-8").strip()
        token_ts = int(ts_text)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"LAB token at {token_path} is malformed: {exc}. "
            "Expected a single line containing a unix timestamp."
        ) from exc

    token_time = datetime.fromtimestamp(token_ts, tz=UTC)
    age = datetime.now(tz=UTC) - token_time
    if age > LAB_TOKEN_MAX_AGE:
        raise ValueError(
            f"LAB consent token is stale (age={age}, max={LAB_TOKEN_MAX_AGE}). "
            "Refresh consent by recreating the token."
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance for production use."""

    return Settings()
