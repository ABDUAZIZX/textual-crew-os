"""Safe ``subprocess.run`` wrapper.

Hardening provided over the stdlib call:

* Accepts an argv list only - ``shell=True`` is impossible from here.
* Resolves the binary against ``PATH`` (or rejects an absolute non-exec).
* Optional allowlist to constrain callable binaries per-call site.
* Minimal default environment; explicit extras must be opt-in strings.
* Mandatory timeout; ``TimeoutExpired`` is converted to a typed error.
* Bounded stdout/stderr capture, with a ``truncated`` flag.
* No silent failures - every error becomes a :class:`SubprocessError`.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - intentional wrapper; usage hardened below
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from crew_os.core.exceptions import SubprocessError

DEFAULT_SAFE_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
)


@dataclass(frozen=True)
class SubprocessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool


def _validate_argv(argv: Sequence[str]) -> None:
    if not argv:
        raise SubprocessError("argv must not be empty")
    if not all(isinstance(a, str) for a in argv):
        raise SubprocessError("every argv element must be a str")
    if any("\x00" in a for a in argv):
        raise SubprocessError("argv elements must not contain NUL bytes")


def _validate_env(extra_env: Mapping[str, str] | None) -> dict[str, str]:
    env: dict[str, str] = {k: os.environ[k] for k in DEFAULT_SAFE_ENV_KEYS if k in os.environ}
    if not extra_env:
        return env
    for k, v in extra_env.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise SubprocessError("env keys and values must be str")
        if "\x00" in k or "\x00" in v:
            raise SubprocessError("env keys/values must not contain NUL bytes")
        env[k] = v
    return env


def _resolve_binary(binary: str) -> str:
    if "/" in binary:
        path = Path(binary)
        if not path.is_file():
            raise SubprocessError(f"binary {binary!r} does not exist")
        if not os.access(binary, os.X_OK):
            raise SubprocessError(f"binary {binary!r} is not executable")
        return binary
    resolved = shutil.which(binary)
    if resolved is None:
        raise SubprocessError(f"binary {binary!r} not found on PATH")
    return resolved


def run_safe(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
    max_output_bytes: int = 1_000_000,
    binary_allowlist: frozenset[str] | set[str] | None = None,
    check: bool = False,
) -> SubprocessResult:
    """Run a subprocess with hardened defaults.

    Args:
        argv: command and arguments (no shell).
        timeout: seconds; mandatory to prevent runaways.
        cwd: working directory or ``None`` to inherit.
        extra_env: additional env to merge on top of the safe defaults.
        max_output_bytes: per-stream cap; output beyond is truncated.
        binary_allowlist: if given, ``argv[0]`` must be a member.
        check: raise :class:`SubprocessError` on non-zero exit.

    Raises:
        SubprocessError: on validation failure, launch failure, or timeout.
    """

    _validate_argv(argv)

    if timeout <= 0:
        raise SubprocessError(f"timeout must be > 0, got {timeout}")
    if max_output_bytes <= 0:
        raise SubprocessError(f"max_output_bytes must be > 0, got {max_output_bytes}")

    binary = argv[0]
    if binary_allowlist is not None and binary not in binary_allowlist:
        raise SubprocessError(f"binary {binary!r} not in allowlist {sorted(binary_allowlist)!r}")

    _resolve_binary(binary)
    env = _validate_env(extra_env)

    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603  # nosec B603 - argv validated; shell=False enforced
            list(argv),
            capture_output=True,
            text=False,
            cwd=str(cwd) if cwd else None,
            env=env,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SubprocessError(f"timeout after {timeout}s running {list(argv)!r}") from exc
    except OSError as exc:
        raise SubprocessError(f"failed to launch {binary!r}: {exc}") from exc

    duration_ms = int((time.monotonic() - start) * 1000)
    stdout_b = proc.stdout or b""
    stderr_b = proc.stderr or b""
    truncated = False
    if len(stdout_b) > max_output_bytes:
        stdout_b = stdout_b[:max_output_bytes]
        truncated = True
    if len(stderr_b) > max_output_bytes:
        stderr_b = stderr_b[:max_output_bytes]
        truncated = True

    result = SubprocessResult(
        argv=tuple(argv),
        returncode=proc.returncode,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
        duration_ms=duration_ms,
        truncated=truncated,
    )

    if check and result.returncode != 0:
        raise SubprocessError(
            f"command exited {result.returncode}: argv={list(argv)!r}; "
            f"stderr={result.stderr[:200]!r}"
        )
    return result
