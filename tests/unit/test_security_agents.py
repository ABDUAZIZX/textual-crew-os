"""Tests for the defensive, offensive, and auditor agents."""

from __future__ import annotations

from pathlib import Path

import pytest

from crew_os.agents.auditor import AuditorAgent, build_garak_argv
from crew_os.agents.sec_defensive import SecDefensiveAgent
from crew_os.agents.sec_offensive import SecOffensiveAgent
from crew_os.core.exceptions import LabModeError, PolicyViolation
from crew_os.core.models import Task, TaskStatus, TraceContext
from crew_os.core.policy import PolicyEngine
from crew_os.llm.ollama_client import GenerateResult
from crew_os.security.audit import AuditLogger
from crew_os.security.rate_limit import RateLimiter


class FakeManager:
    def __init__(self, text: str = "analysis") -> None:
        self._text = text

    async def generate(
        self, model: str, prompt: str, *, system: str | None = None
    ) -> GenerateResult:
        return GenerateResult(model=model, response=self._text)


def _trace() -> TraceContext:
    return TraceContext.new_root()


# ─────────────────────────────── defensive ─────────────────────────────


async def test_defensive_handle_task(tmp_path: Path) -> None:
    agent = SecDefensiveAgent(
        policy=PolicyEngine(),
        rate_limiter=RateLimiter(),
        auditor=AuditLogger(tmp_path / "a.jsonl"),
        read_root=tmp_path,
        model_manager=FakeManager("found: hardcoded secret"),  # type: ignore[arg-type]
    )
    task = Task(description="review auth module", trace=_trace())
    done = await agent.handle_task(task)
    assert done.status == TaskStatus.COMPLETED
    assert "hardcoded secret" in done.result["review"]  # type: ignore[index]


async def test_defensive_read_file_path_guard(tmp_path: Path) -> None:
    agent = SecDefensiveAgent(
        policy=PolicyEngine(),
        rate_limiter=RateLimiter(),
        auditor=AuditLogger(tmp_path / "a.jsonl"),
        read_root=tmp_path / "sub",
    )
    (tmp_path / "sub").mkdir()
    with pytest.raises(ValueError, match="escapes"):
        await agent.invoke_tool("read_file", {"path": "../secret"}, _trace())


# ─────────────────────────────── offensive ─────────────────────────────


def _offensive(tmp_path: Path, *, lab: bool, confirm: bool = True) -> SecOffensiveAgent:
    return SecOffensiveAgent(
        policy=PolicyEngine(),
        rate_limiter=RateLimiter(),
        auditor=AuditLogger(tmp_path / "off.jsonl"),
        model_manager=FakeManager("offensive plan"),  # type: ignore[arg-type]
        lab_mode_active=lab,
        confirm_fn=lambda **_: confirm,
    )


async def test_offensive_refused_without_lab(tmp_path: Path) -> None:
    agent = _offensive(tmp_path, lab=False)
    task = Task(description="scan target", trace=_trace())
    with pytest.raises(LabModeError, match="without LAB_MODE"):
        await agent.handle_task(task)


async def test_offensive_refused_when_operator_denies(tmp_path: Path) -> None:
    agent = _offensive(tmp_path, lab=True, confirm=False)
    task = Task(description="scan target", trace=_trace())
    with pytest.raises(LabModeError, match="denied"):
        await agent.handle_task(task)


async def test_offensive_runs_when_lab_and_confirmed(tmp_path: Path) -> None:
    agent = _offensive(tmp_path, lab=True, confirm=True)
    task = Task(description="scan target", trace=_trace())
    done = await agent.handle_task(task)
    assert done.status == TaskStatus.COMPLETED
    assert done.result["plan"] == "offensive plan"  # type: ignore[index]
    assert agent.confirmed is True


async def test_offensive_confirms_only_once(tmp_path: Path) -> None:
    calls = {"n": 0}

    def confirm(**_: object) -> bool:
        calls["n"] += 1
        return True

    agent = SecOffensiveAgent(
        policy=PolicyEngine(),
        rate_limiter=RateLimiter(),
        auditor=AuditLogger(tmp_path / "off.jsonl"),
        model_manager=FakeManager("plan"),  # type: ignore[arg-type]
        lab_mode_active=True,
        confirm_fn=confirm,
    )
    await agent.handle_task(Task(description="t1", trace=_trace()))
    await agent.handle_task(Task(description="t2", trace=_trace()))
    assert calls["n"] == 1


async def test_offensive_tool_blocked_by_policy_without_lab(tmp_path: Path) -> None:
    # Even if handle_task were bypassed, the tool itself is policy-gated.
    agent = _offensive(tmp_path, lab=False)
    with pytest.raises(PolicyViolation):
        await agent.invoke_tool("offensive_probe", {"target": "x"}, _trace())


# ─────────────────────────────── auditor ───────────────────────────────


def test_build_garak_argv_valid(tmp_path: Path) -> None:
    argv = build_garak_argv(
        Path("/venv/bin/python"),
        model_name="llama3.1:8b",
        probes=["dan.Dan_11_0", "encoding"],
        report_prefix=tmp_path / "rep",
    )
    assert argv[0] == "/venv/bin/python"
    assert "--probes" in argv
    assert argv[argv.index("--probes") + 1] == "dan.Dan_11_0,encoding"


@pytest.mark.parametrize("bad", ["evil; rm -rf", "probe space", "$(whoami)", ""])
def test_build_garak_argv_rejects_bad_probe(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="probe"):
        build_garak_argv(
            Path("/venv/bin/python"),
            model_name="llama3.1:8b",
            probes=[bad],
            report_prefix=tmp_path / "rep",
        )


def test_build_garak_argv_rejects_bad_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="model"):
        build_garak_argv(
            Path("/venv/bin/python"),
            model_name="bad name; evil",
            probes=["encoding"],
            report_prefix=tmp_path / "rep",
        )


def test_build_garak_argv_requires_probes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one probe"):
        build_garak_argv(
            Path("/venv/bin/python"),
            model_name="llama3.1:8b",
            probes=[],
            report_prefix=tmp_path / "rep",
        )


async def test_auditor_run_garak_via_echo(tmp_path: Path) -> None:
    # Point "garak_python" at /bin/echo so run_safe actually executes and
    # we exercise the subprocess path without garak installed.
    agent = AuditorAgent(
        policy=PolicyEngine(),
        rate_limiter=RateLimiter(),
        auditor=AuditLogger(tmp_path / "aud.jsonl"),
        garak_python=Path("/bin/echo"),
        report_dir=tmp_path / "reports",
        garak_timeout=10.0,
    )
    result = await agent.invoke_tool(
        "run_garak",
        {"model_name": "llama3.1:8b", "probes": ["encoding"]},
        _trace(),
    )
    assert result["ok"] is True
    assert "garak" in result["stdout"]


async def test_auditor_run_garak_rejects_bad_probe(tmp_path: Path) -> None:
    agent = AuditorAgent(
        policy=PolicyEngine(),
        rate_limiter=RateLimiter(),
        auditor=AuditLogger(tmp_path / "aud.jsonl"),
        garak_python=Path("/bin/echo"),
        report_dir=tmp_path / "reports",
    )
    with pytest.raises(ValueError, match="probe"):
        await agent.invoke_tool(
            "run_garak",
            {"model_name": "llama3.1:8b", "probes": ["bad; evil"]},
            _trace(),
        )


async def test_auditor_handle_task_ready(tmp_path: Path) -> None:
    agent = AuditorAgent(
        policy=PolicyEngine(),
        rate_limiter=RateLimiter(),
        auditor=AuditLogger(tmp_path / "aud.jsonl"),
        garak_python=Path("/bin/echo"),
        report_dir=tmp_path / "reports",
    )
    done = await agent.handle_task(Task(description="audit the model", trace=_trace()))
    assert done.status == TaskStatus.COMPLETED
