#!/usr/bin/env python3
"""Environment health check for Textual Crew OS.

Run before the first `crew` invocation. Verifies:

- Python version >= 3.11
- Ollama API reachable on the configured host
- Ollama bound to loopback only (not 0.0.0.0)
- NVIDIA GPU present and has enough free VRAM for a 7B Q4 model
- Required Ollama models present locally
- LAB consent token (if present) is sane

Exit codes:
    0  - all checks OK or only warnings
    1  - at least one critical check failed
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Literal

REQUIRED_PYTHON: tuple[int, int] = (3, 11)


def _validated_ollama_host() -> str:
    host = os.environ.get("CREW_OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    # Enforce http(s) scheme: urllib.urlopen also honors file://, so a non-http
    # value in CREW_OLLAMA_HOST could turn an API probe into an arbitrary file read.
    if not host.startswith(("http://", "https://")):
        raise ValueError(f"CREW_OLLAMA_HOST must start with http:// or https:// — got {host!r}")
    return host


OLLAMA_HOST: str = _validated_ollama_host()
MIN_VRAM_MB: int = 6000  # ~6 GB free required for a 7B Q4_K_M with KV cache.

REQUIRED_MODELS: tuple[str, ...] = (
    "qwen2.5:3b",
    "qwen2.5-coder:7b-instruct-q4_K_M",
    "llama3.1:8b-instruct-q4_K_M",
    "nomic-embed-text:latest",
)

LAB_TOKEN_PATH: str = os.path.expanduser("~/.crew/lab_consent.token")

Status = Literal["ok", "warn", "fail"]
STATUS_LABEL: dict[Status, str] = {"ok": " OK ", "warn": "WARN", "fail": "FAIL"}


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str


# ─────────────────────────────── checks ────────────────────────────────


def check_python() -> CheckResult:
    if sys.version_info[:2] < REQUIRED_PYTHON:
        return CheckResult(
            "python",
            "fail",
            f"need >= {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}, "
            f"got {sys.version_info[0]}.{sys.version_info[1]}",
        )
    return CheckResult("python", "ok", sys.version.split()[0])


def check_ollama_reachable() -> CheckResult:
    url = f"{OLLAMA_HOST}/api/version"
    try:
        # Host scheme is enforced in _validated_ollama_host(), so file:// cannot reach here.
        # nosemgrep
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            data = json.loads(resp.read())
    except urllib.error.URLError as exc:
        return CheckResult("ollama-api", "fail", f"unreachable at {url}: {exc}")
    except (OSError, ValueError) as exc:
        return CheckResult("ollama-api", "fail", f"bad response from {url}: {exc}")
    version = data.get("version", "?") if isinstance(data, dict) else "?"
    return CheckResult("ollama-api", "ok", f"version {version} at {OLLAMA_HOST}")


_BIND_RE = re.compile(r"(?P<addr>(\d{1,3}\.){3}\d{1,3}|\[[^\]]+\]|\*):(?P<port>\d+)\b")


def check_ollama_binding() -> CheckResult:
    """Confirm the Ollama port is bound to loopback, not 0.0.0.0."""

    ss = shutil.which("ss")
    if not ss:
        return CheckResult("ollama-bind", "warn", "`ss` not on PATH; skipped")
    try:
        result = subprocess.run(
            [ss, "-tlnH"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult("ollama-bind", "warn", f"ss failed: {exc}")
    if result.returncode != 0:
        return CheckResult("ollama-bind", "warn", f"ss exit {result.returncode}")

    bindings: list[str] = []
    for line in result.stdout.splitlines():
        for m in _BIND_RE.finditer(line):
            if m.group("port") == "11434":
                bindings.append(m.group(0))

    if not bindings:
        return CheckResult("ollama-bind", "warn", "no listener on :11434 found")

    public = [b for b in bindings if b.startswith(("0.0.0.0:", "*:", "[::]:"))]
    if public:
        return CheckResult(
            "ollama-bind",
            "fail",
            f"Ollama is bound publicly at {', '.join(public)} - exposes GPU + models. "
            "Fix: set OLLAMA_HOST=127.0.0.1:11434 in the systemd unit and restart.",
        )
    return CheckResult("ollama-bind", "ok", f"loopback ({', '.join(bindings)})")


def check_gpu() -> CheckResult:  # noqa: PLR0911 - flat error-handling reads cleaner
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return CheckResult("gpu", "fail", "nvidia-smi not on PATH")
    try:
        result = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=name,memory.free,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except subprocess.CalledProcessError as exc:
        return CheckResult("gpu", "fail", f"nvidia-smi failed: {exc.stderr.strip()}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult("gpu", "fail", f"nvidia-smi error: {exc}")

    first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    parts = [p.strip() for p in first_line.split(",")]
    if len(parts) != 3:
        return CheckResult("gpu", "fail", f"unexpected nvidia-smi output: {first_line!r}")
    name, free_mb, total_mb = parts
    try:
        free, total = int(free_mb), int(total_mb)
    except ValueError:
        return CheckResult("gpu", "fail", f"non-numeric vram: {free_mb}/{total_mb}")

    if free < MIN_VRAM_MB:
        return CheckResult(
            "gpu",
            "warn",
            f"{name} - {free} MB free / {total} MB total (need >= {MIN_VRAM_MB} MB for 7B Q4)",
        )
    return CheckResult("gpu", "ok", f"{name} - {free} MB free / {total} MB total")


def check_models() -> CheckResult:
    url = f"{OLLAMA_HOST}/api/tags"
    try:
        # Host scheme is enforced in _validated_ollama_host(), so file:// cannot reach here.
        # nosemgrep
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return CheckResult("models", "warn", f"could not list models: {exc}")

    installed = {m.get("name", "") for m in data.get("models", []) if isinstance(m, dict)}
    missing = [m for m in REQUIRED_MODELS if m not in installed]
    if missing:
        return CheckResult(
            "models",
            "warn",
            f"missing: {', '.join(missing)}. Run scripts/verify_models.sh for hints.",
        )
    return CheckResult("models", "ok", f"{len(REQUIRED_MODELS)} core models present")


def check_lab_token() -> CheckResult:
    if not os.path.exists(LAB_TOKEN_PATH):
        return CheckResult("lab-token", "ok", "absent (LAB_MODE disabled)")
    mode = os.stat(LAB_TOKEN_PATH).st_mode & 0o777
    if mode & 0o077:
        return CheckResult(
            "lab-token",
            "warn",
            f"present at {LAB_TOKEN_PATH} but mode={oct(mode)} (need 0600)",
        )
    return CheckResult("lab-token", "ok", f"present, mode={oct(mode)}")


# ─────────────────────────────── runner ────────────────────────────────


def run_checks() -> list[CheckResult]:
    return [
        check_python(),
        check_ollama_reachable(),
        check_ollama_binding(),
        check_gpu(),
        check_models(),
        check_lab_token(),
    ]


def main() -> int:
    results = run_checks()
    width = max(len(r.name) for r in results)

    sys.stdout.write("Textual Crew OS - environment check\n\n")
    for r in results:
        sys.stdout.write(f"  [{STATUS_LABEL[r.status]}] {r.name.ljust(width)}  {r.detail}\n")

    sys.stdout.write("\n")
    if any(r.status == "fail" for r in results):
        sys.stdout.write("Status: FAIL - fix critical issues before running crew.\n")
        return 1
    if any(r.status == "warn" for r in results):
        sys.stdout.write("Status: WARN - crew can start with reduced capabilities.\n")
        return 0
    sys.stdout.write("Status: OK - environment ready.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
