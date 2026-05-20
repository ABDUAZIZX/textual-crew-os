"""Shared pytest fixtures for Textual Crew OS."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a fresh data dir and clear sensitive env vars.

    Running the suite must never read or mutate the user's real
    ``$HOME/.crew/lab_consent.token`` or their actual Anthropic key.
    """

    monkeypatch.setenv("CREW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CREW_LAB_MODE", raising=False)
    monkeypatch.delenv("CREW_ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Provide a writable, isolated data directory for a single test."""

    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d
