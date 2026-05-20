"""Smoke and security tests for ``crew_os.config``.

These exercise the most security-critical invariants:

* loopback binding for web + Ollama,
* LAB_MODE multi-gate (token presence, permissions, freshness).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from crew_os import config as cfg
from crew_os.config import Settings


def test_defaults_load() -> None:
    s = Settings()
    assert s.web_host == "127.0.0.1"
    assert s.web_port == 8765
    assert s.lab_mode is False
    assert s.anthropic_api_key is None


@pytest.mark.parametrize("bad_host", ["0.0.0.0", "192.168.1.10", "example.com"])
def test_web_host_must_be_loopback(monkeypatch: pytest.MonkeyPatch, bad_host: str) -> None:
    monkeypatch.setenv("CREW_WEB_HOST", bad_host)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://0.0.0.0:11434",
        "http://192.168.1.10:11434",
        "ftp://127.0.0.1:11434",
        "not-a-url",
    ],
)
def test_ollama_host_must_be_loopback(monkeypatch: pytest.MonkeyPatch, bad_url: str) -> None:
    monkeypatch.setenv("CREW_OLLAMA_HOST", bad_url)
    with pytest.raises(ValidationError):
        Settings()


def test_lab_mode_disabled_does_not_check_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cfg, "LAB_TOKEN_PATH", tmp_path / "missing.token")
    monkeypatch.setenv("CREW_LAB_MODE", "0")
    s = Settings()
    assert s.lab_mode is False


def test_lab_mode_requires_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cfg, "LAB_TOKEN_PATH", tmp_path / "missing.token")
    monkeypatch.setenv("CREW_LAB_MODE", "1")
    with pytest.raises(ValidationError, match="consent token not found"):
        Settings()


def test_lab_mode_rejects_stale_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "consent.token"
    stale = int(time.time()) - int(cfg.LAB_TOKEN_MAX_AGE.total_seconds()) - 3600
    token.write_text(str(stale))
    token.chmod(0o600)
    monkeypatch.setattr(cfg, "LAB_TOKEN_PATH", token)
    monkeypatch.setenv("CREW_LAB_MODE", "1")
    with pytest.raises(ValidationError, match="stale"):
        Settings()


def test_lab_mode_rejects_loose_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = tmp_path / "consent.token"
    token.write_text(str(int(time.time())))
    token.chmod(0o644)
    monkeypatch.setattr(cfg, "LAB_TOKEN_PATH", token)
    monkeypatch.setenv("CREW_LAB_MODE", "1")
    with pytest.raises(ValidationError, match="loose permissions"):
        Settings()


def test_lab_mode_rejects_malformed_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "consent.token"
    token.write_text("not-a-timestamp")
    token.chmod(0o600)
    monkeypatch.setattr(cfg, "LAB_TOKEN_PATH", token)
    monkeypatch.setenv("CREW_LAB_MODE", "1")
    with pytest.raises(ValidationError, match="malformed"):
        Settings()


def test_lab_mode_accepts_valid_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "consent.token"
    token.write_text(str(int(time.time())))
    token.chmod(0o600)
    monkeypatch.setattr(cfg, "LAB_TOKEN_PATH", token)
    monkeypatch.setenv("CREW_LAB_MODE", "1")
    s = Settings()
    assert s.lab_mode is True


def test_ensure_dirs_creates_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "crew-data"
    monkeypatch.setenv("CREW_DATA_DIR", str(target))
    s = Settings()
    s.ensure_dirs()
    assert (target / "audit").is_dir()
    assert (target / "traces").is_dir()
    assert (target / "memory").is_dir()
