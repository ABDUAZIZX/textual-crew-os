"""Tests for ``crew_os.security.sandbox``.

The ``build_bwrap_argv`` function is pure and exercised heavily;
the integration tests at the bottom run actual bwrap subprocesses if it
is installed (and ``skip`` otherwise).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from crew_os.core.exceptions import SandboxError
from crew_os.security.sandbox import build_bwrap_argv, is_available, run_sandboxed

bwrap_required = pytest.mark.skipif(
    not is_available(), reason="bwrap is not installed on this host"
)


# ─────────────────────────────── pure builder ──────────────────────────


def test_argv_starts_with_bwrap_and_inner_argv_at_tail() -> None:
    argv = build_bwrap_argv("/usr/bin/bwrap", ["/bin/echo", "hi"])
    assert argv[0] == "/usr/bin/bwrap"
    assert argv[-2:] == ["/bin/echo", "hi"]


def test_argv_unshares_all_and_no_share_net_by_default() -> None:
    argv = build_bwrap_argv("/usr/bin/bwrap", ["/bin/true"])
    assert "--unshare-all" in argv
    assert "--share-net" not in argv
    assert "--die-with-parent" in argv
    assert "--new-session" in argv


def test_network_flag_adds_share_net() -> None:
    argv = build_bwrap_argv("/usr/bin/bwrap", ["/bin/true"], network=True)
    assert "--share-net" in argv


def test_writable_paths_become_rw_binds() -> None:
    argv = build_bwrap_argv(
        "/usr/bin/bwrap",
        ["/bin/true"],
        writable=[Path("/data/scratch"), Path("/work")],
    )
    # Each writable path appears as two consecutive entries after --bind.
    pairs = [(argv[i + 1], argv[i + 2]) for i, tok in enumerate(argv[:-2]) if tok == "--bind"]
    assert ("/data/scratch", "/data/scratch") in pairs
    assert ("/work", "/work") in pairs


def test_relative_writable_path_rejected() -> None:
    with pytest.raises(SandboxError, match="absolute"):
        build_bwrap_argv("/usr/bin/bwrap", ["/bin/true"], writable=[Path("rel/path")])


def test_writable_must_be_path_objects() -> None:
    with pytest.raises(SandboxError, match="Path"):
        build_bwrap_argv(
            "/usr/bin/bwrap",
            ["/bin/true"],
            writable=["/abs/string"],  # type: ignore[list-item]
        )


def test_empty_inner_argv_rejected() -> None:
    with pytest.raises(SandboxError, match="empty"):
        build_bwrap_argv("/usr/bin/bwrap", [])


def test_relative_cwd_rejected() -> None:
    with pytest.raises(SandboxError, match="absolute"):
        build_bwrap_argv("/usr/bin/bwrap", ["/bin/true"], cwd=Path("./relative"))


def test_cwd_appended_as_chdir() -> None:
    argv = build_bwrap_argv("/usr/bin/bwrap", ["/bin/true"], cwd=Path("/tmp"))
    assert "--chdir" in argv
    chdir_idx = argv.index("--chdir")
    assert argv[chdir_idx + 1] == "/tmp"


# ─────────────────────────────── integration ───────────────────────────


@bwrap_required
def test_run_sandboxed_echo_inside_namespace() -> None:
    result = run_sandboxed(["/bin/echo", "isolated"], timeout=10.0)
    assert result.returncode == 0
    assert "isolated" in result.stdout


@bwrap_required
def test_sandbox_blocks_network_by_default() -> None:
    # Inside the sandbox, no network → DNS resolution should fail fast.
    result = run_sandboxed(
        ["/usr/bin/getent", "hosts", "example.com"],
        timeout=10.0,
    )
    # getent returns non-zero on resolution failure.
    assert result.returncode != 0


@bwrap_required
def test_sandbox_writable_path_persists(tmp_path: Path) -> None:
    target = tmp_path / "scratch"
    target.mkdir()
    result = run_sandboxed(
        ["/bin/sh", "-c", f"echo hi > {target}/out.txt"],
        timeout=10.0,
        writable=[target],
    )
    assert result.returncode == 0
    assert (target / "out.txt").read_text().strip() == "hi"


def test_run_sandboxed_when_bwrap_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SandboxError, match="not installed"):
        run_sandboxed(["/bin/true"], timeout=1.0)
