"""Tests for ``crew_os.orchestration.supervisor_tier1`` - deterministic routing."""

from __future__ import annotations

import pytest

from crew_os.core.models import AgentRole, Task, TraceContext
from crew_os.orchestration.supervisor_tier1 import (
    SupervisorT1,
    TaskCategory,
)


def _task(desc: str) -> Task:
    return Task(description=desc, trace=TraceContext.new_root())


@pytest.fixture
def t1() -> SupervisorT1:
    return SupervisorT1(escalation_threshold=7)


def test_rejects_bad_threshold() -> None:
    with pytest.raises(ValueError, match="threshold"):
        SupervisorT1(escalation_threshold=11)


@pytest.mark.parametrize(
    ("desc", "expected"),
    [
        ("implement a function and add a unit test", TaskCategory.CODING),
        ("harden the ssh config against cve exploits", TaskCategory.SECURITY_DEFENSIVE),
        ("run a pentest and craft an exploit payload", TaskCategory.SECURITY_OFFENSIVE),
        ("run garak probe to red team the model", TaskCategory.AUDIT),
        ("please water the plants", TaskCategory.GENERAL),
    ],
)
def test_classify(t1: SupervisorT1, desc: str, expected: TaskCategory) -> None:
    assert t1.classify(_task(desc)) == expected


def test_classify_priority_offensive_over_defensive(t1: SupervisorT1) -> None:
    # Contains both "secure" (defensive) and "exploit" (offensive);
    # offensive wins the priority tiebreak.
    cat = t1.classify(_task("secure the app then write an exploit"))
    assert cat == TaskCategory.SECURITY_OFFENSIVE


def test_route_maps_category_to_role(t1: SupervisorT1) -> None:
    assert t1.route(_task("implement a class")).target_role == AgentRole.CODER
    assert t1.route(_task("harden the firewall")).target_role == AgentRole.SEC_DEFENSIVE
    assert t1.route(_task("craft an exploit payload")).target_role == AgentRole.SEC_OFFENSIVE
    assert t1.route(_task("garak probe audit")).target_role == AgentRole.AUDITOR


def test_simple_task_does_not_escalate(t1: SupervisorT1) -> None:
    decision = t1.route(_task("fix a small bug"))
    assert decision.complexity < 7
    assert decision.escalate_to_t2 is False


def test_complex_task_escalates(t1: SupervisorT1) -> None:
    desc = (
        "design the end-to-end distributed architecture, then implement the "
        "core modules, then migrate the entire data layer "
    ) + "word " * 120
    decision = t1.route(_task(desc))
    assert decision.complexity >= 7
    assert decision.escalate_to_t2 is True


def test_offensive_base_complexity_higher() -> None:
    t1 = SupervisorT1(escalation_threshold=3)
    decision = t1.route(_task("write an exploit payload"))
    # Offensive base (3) alone reaches the low threshold.
    assert decision.complexity >= 3
    assert decision.escalate_to_t2 is True


def test_custom_score_fn_is_used() -> None:
    t1 = SupervisorT1(escalation_threshold=5, score_fn=lambda _t, _c: 9)
    decision = t1.route(_task("anything"))
    assert decision.complexity == 9
    assert decision.escalate_to_t2 is True


def test_score_is_clamped_to_max() -> None:
    t1 = SupervisorT1(score_fn=lambda _t, _c: 999)
    decision = t1.route(_task("x"))
    assert decision.complexity == 10


def test_reason_is_populated(t1: SupervisorT1) -> None:
    decision = t1.route(_task("implement a function"))
    assert "category=" in decision.reason
    assert "complexity=" in decision.reason
