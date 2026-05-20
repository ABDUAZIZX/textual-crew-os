"""Bubblewrap-based subprocess sandbox.

We run untrusted subprocesses (compiler invocations, code samples, security
tooling) inside a minimal bwrap namespace:

* Read-only bind of system directories (/usr, /lib, /etc subset).
* Fresh /tmp and /run as tmpfs.
* Network unshared by default; opt-in via ``network=True``.
* All other namespaces unshared (PID, IPC, UTS, cgroup).
* ``--die-with-parent`` ensures cleanup if the supervisor crashes.

Writable paths are bound read-write only when the caller explicitly
asks for them, and must be absolute. The full bwrap argv is built by a
pure :func:`build_bwrap_argv` to make unit-testing straightforward.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

from crew_os.core.exceptions import SandboxError
from crew_os.security.subprocess_safe import SubprocessResult, run_safe

_RO_BIND: tuple[tuple[str, str], ...] = (
    ("/usr", "/usr"),
    ("/bin", "/bin"),
)
_RO_BIND_TRY: tuple[tuple[str, str], ...] = (
    ("/lib", "/lib"),
    ("/lib32", "/lib32"),
    ("/lib64", "/lib64"),
    ("/sbin", "/sbin"),
    ("/etc/alternatives", "/etc/alternatives"),
    ("/etc/ssl", "/etc/ssl"),
    ("/etc/ca-certificates", "/etc/ca-certificates"),
    ("/etc/resolv.conf", "/etc/resolv.conf"),
)


def is_available() -> bool:
    """Return True if ``bwrap`` is installed on PATH."""

    return shutil.which("bwrap") is not None


def build_bwrap_argv(
    bwrap_path: str,
    inner_argv: Sequence[str],
    *,
    writable: Sequence[Path] = (),
    network: bool = False,
    cwd: Path | None = None,
) -> list[str]:
    """Build the bwrap argv for the given inner command. Pure function."""

    if not inner_argv:
        raise SandboxError("inner_argv must not be empty")
    for path in writable:
        if not isinstance(path, Path):
            raise SandboxError(f"writable entry must be a Path, got {type(path).__name__}")
        if not path.is_absolute():
            raise SandboxError(f"writable path must be absolute: {path}")

    argv: list[str] = [
        bwrap_path,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",  # noqa: S108  # nosec B108 - tmpfs path inside sandbox, not host
        "--tmpfs",
        "/run",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/tmp",  # noqa: S108  # nosec B108 - HOME inside sandbox tmpfs
    ]
    for src, dst in _RO_BIND:
        argv.extend(["--ro-bind", src, dst])
    for src, dst in _RO_BIND_TRY:
        argv.extend(["--ro-bind-try", src, dst])
    if network:
        argv.append("--share-net")
    for path in writable:
        argv.extend(["--bind", str(path), str(path)])
    if cwd is not None:
        if not cwd.is_absolute():
            raise SandboxError(f"cwd must be absolute: {cwd}")
        argv.extend(["--chdir", str(cwd)])
    argv.extend(inner_argv)
    return argv


def run_sandboxed(
    inner_argv: Sequence[str],
    *,
    timeout: float,
    writable: Sequence[Path] = (),
    network: bool = False,
    cwd: Path | None = None,
    max_output_bytes: int = 1_000_000,
) -> SubprocessResult:
    """Run ``inner_argv`` inside a bubblewrap sandbox."""

    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise SandboxError(
            "bubblewrap (bwrap) is not installed. Install with: sudo apt install -y bubblewrap"
        )
    argv = build_bwrap_argv(bwrap, inner_argv, writable=writable, network=network, cwd=cwd)
    return run_safe(argv, timeout=timeout, max_output_bytes=max_output_bytes)
