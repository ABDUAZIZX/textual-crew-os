"""Tests for ``crew_os.security.subprocess_safe``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from crew_os.core.exceptions import SubprocessError
from crew_os.security.subprocess_safe import run_safe

# ─────────────────────────────── validation ────────────────────────────


def test_rejects_empty_argv() -> None:
    with pytest.raises(SubprocessError, match="empty"):
        run_safe([], timeout=1.0)


def test_rejects_non_string_argv() -> None:
    with pytest.raises(SubprocessError, match="str"):
        run_safe(["/bin/echo", 1], timeout=1.0)  # type: ignore[list-item]


def test_rejects_nul_byte_in_argv() -> None:
    with pytest.raises(SubprocessError, match="NUL"):
        run_safe(["/bin/echo", "bad\x00arg"], timeout=1.0)


def test_rejects_nonexistent_binary() -> None:
    with pytest.raises(SubprocessError, match="not found"):
        run_safe(["definitely-not-a-real-binary-xyz"], timeout=1.0)


def test_rejects_absolute_nonexistent_binary(tmp_path: Path) -> None:
    fake = tmp_path / "ghost"
    with pytest.raises(SubprocessError, match="does not exist"):
        run_safe([str(fake)], timeout=1.0)


def test_rejects_non_executable_absolute(tmp_path: Path) -> None:
    plain = tmp_path / "plain.txt"
    plain.write_text("not executable")
    with pytest.raises(SubprocessError, match="not executable"):
        run_safe([str(plain)], timeout=1.0)


def test_rejects_zero_timeout() -> None:
    with pytest.raises(SubprocessError, match="timeout"):
        run_safe(["/bin/echo", "hi"], timeout=0)


def test_rejects_negative_max_output() -> None:
    with pytest.raises(SubprocessError, match="max_output_bytes"):
        run_safe(["/bin/echo", "hi"], timeout=1.0, max_output_bytes=0)


def test_binary_allowlist_enforced() -> None:
    with pytest.raises(SubprocessError, match="allowlist"):
        run_safe(["/bin/echo", "hi"], timeout=1.0, binary_allowlist={"/bin/true"})


# ─────────────────────────────── execution ─────────────────────────────


def test_runs_echo_and_captures_stdout() -> None:
    result = run_safe(["/bin/echo", "hello"], timeout=5.0)
    assert result.returncode == 0
    assert "hello" in result.stdout
    assert result.stderr == ""
    assert result.duration_ms >= 0
    assert result.argv == ("/bin/echo", "hello")
    assert result.truncated is False


def test_truncation_when_output_exceeds_cap() -> None:
    # Produce ~2KB of output, cap at 100 bytes.
    result = run_safe(
        ["/bin/sh", "-c", "printf 'a%.0s' $(seq 1 2000)"],
        timeout=5.0,
        max_output_bytes=100,
    )
    assert result.returncode == 0
    assert len(result.stdout) <= 100
    assert result.truncated is True


def test_check_raises_on_nonzero_exit() -> None:
    with pytest.raises(SubprocessError, match="exited 1"):
        run_safe(["/bin/sh", "-c", "exit 1"], timeout=5.0, check=True)


def test_check_false_returns_nonzero_normally() -> None:
    result = run_safe(["/bin/sh", "-c", "exit 7"], timeout=5.0)
    assert result.returncode == 7


def test_timeout_converted_to_subprocess_error() -> None:
    with pytest.raises(SubprocessError, match="timeout"):
        run_safe(["/bin/sleep", "5"], timeout=0.1)


def test_env_passes_extras_and_strips_others(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREW_SECRET_LEAK", "do-not-leak")
    result = run_safe(
        ["/usr/bin/env"],
        timeout=5.0,
        extra_env={"CREW_VISIBLE": "yes"},
    )
    assert "CREW_VISIBLE=yes" in result.stdout
    assert "CREW_SECRET_LEAK" not in result.stdout


def test_env_rejects_nul_bytes() -> None:
    with pytest.raises(SubprocessError, match="NUL"):
        run_safe(
            ["/bin/echo", "x"],
            timeout=1.0,
            extra_env={"BAD\x00KEY": "v"},
        )


def test_cwd_is_respected(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("ok")
    result = run_safe(["/bin/ls"], timeout=5.0, cwd=tmp_path)
    assert "sentinel" in result.stdout


def test_shell_metacharacters_treated_as_literals() -> None:
    # A real shell would interpret `;`. Our wrapper just passes it as argv,
    # so /bin/echo prints it literally - proves we are not invoking a shell.
    result = run_safe(["/bin/echo", "a; rm -rf /"], timeout=5.0)
    assert "a; rm -rf /" in result.stdout
    assert os.path.exists("/")  # paranoid check
