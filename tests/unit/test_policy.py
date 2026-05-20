"""Tests for ``crew_os.core.policy`` - rule semantics and engine wiring."""

from __future__ import annotations

import pytest

from crew_os.core import policy as pol
from crew_os.core.models import AgentRole, ToolCall, ToolManifest, TraceContext
from crew_os.core.policy import (
    DEFAULT_RULES,
    Decision,
    PolicyDecision,
    PolicyEngine,
    Rule,
)


def _call(role: AgentRole, tool: str = "t") -> ToolCall:
    return ToolCall(tool_name=tool, agent_role=role, trace=TraceContext.new_root())


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine()


# ─────────────────────────────── basic engine ──────────────────────────


def test_engine_is_closed_by_default() -> None:
    engine = PolicyEngine(rules=())
    d = engine.evaluate(_call(AgentRole.CODER), ToolManifest(name="weird", dangerous=True))
    assert d.decision == Decision.DENY
    assert d.rule == "default"


def test_engine_open_default_explicit() -> None:
    engine = PolicyEngine(rules=(), default_decision=Decision.ALLOW)
    d = engine.evaluate(_call(AgentRole.CODER), ToolManifest(name="ok"))
    assert d.decision == Decision.ALLOW


def test_first_matching_rule_wins() -> None:
    deny_first = Rule(
        name="deny-first",
        predicate=lambda _c, _m, _lab: Decision.DENY,
        deny_reason="d",
    )
    allow_after = Rule(
        name="allow-after",
        predicate=lambda _c, _m, _lab: Decision.ALLOW,
    )
    engine = PolicyEngine(rules=(deny_first, allow_after))
    d = engine.evaluate(_call(AgentRole.CODER), ToolManifest(name="x"))
    assert d.rule == "deny-first"
    assert d.decision == Decision.DENY


# ─────────────────────────────── default rules ─────────────────────────


@pytest.mark.parametrize(
    "role",
    [AgentRole.CODER, AgentRole.SEC_DEFENSIVE, AgentRole.AUDITOR, AgentRole.SEC_OFFENSIVE],
)
def test_network_denied_for_non_supervisors(engine: PolicyEngine, role: AgentRole) -> None:
    m = ToolManifest(name="fetch", requires_network=True)
    d = engine.evaluate(_call(role), m, lab_mode_active=True)
    assert d.decision == Decision.DENY
    assert d.rule == "network-egress-supervisor-only"


@pytest.mark.parametrize("role", [AgentRole.SUPERVISOR_T1, AgentRole.SUPERVISOR_T2])
def test_network_allowed_for_supervisors(engine: PolicyEngine, role: AgentRole) -> None:
    # Supervisors clear the network gate and reach the final allow.
    m = ToolManifest(name="fetch", requires_network=True)
    d = engine.evaluate(_call(role), m, lab_mode_active=False)
    assert d.decision == Decision.ALLOW
    assert d.rule == "allow-permitted"


def test_lab_mode_required_blocks_when_inactive(engine: PolicyEngine) -> None:
    m = ToolManifest(name="scan", requires_lab_mode=True, dangerous=True)
    d = engine.evaluate(_call(AgentRole.SEC_OFFENSIVE), m, lab_mode_active=False)
    assert d.decision == Decision.DENY
    # Either the lab-mode rule or the offensive-agent rule could fire first;
    # both are correct denials.
    assert d.rule in {"lab-mode-required", "offensive-agent-requires-lab"}


def test_offensive_agent_blocked_without_lab(engine: PolicyEngine) -> None:
    m = ToolManifest(name="anything")
    d = engine.evaluate(_call(AgentRole.SEC_OFFENSIVE), m, lab_mode_active=False)
    assert d.decision == Decision.DENY
    assert d.rule == "offensive-agent-requires-lab"


def test_dangerous_blocked_for_non_security_role(engine: PolicyEngine) -> None:
    m = ToolManifest(name="rm", dangerous=True)
    d = engine.evaluate(_call(AgentRole.CODER), m)
    assert d.decision == Decision.DENY
    assert d.rule == "dangerous-tool-role-allowlist"


def test_safe_tool_allowed_for_any_role(engine: PolicyEngine) -> None:
    m = ToolManifest(name="read_file")
    for role in AgentRole:
        if role == AgentRole.SEC_OFFENSIVE:
            d = engine.evaluate(_call(role), m, lab_mode_active=False)
            assert d.decision == Decision.DENY  # offensive agent always needs lab
            continue
        d = engine.evaluate(_call(role), m)
        assert d.decision == Decision.ALLOW
        assert d.rule == "allow-permitted"


def test_lab_mode_active_allows_offensive_with_safe_tool(engine: PolicyEngine) -> None:
    m = ToolManifest(name="describe", requires_lab_mode=False)
    d = engine.evaluate(_call(AgentRole.SEC_OFFENSIVE), m, lab_mode_active=True)
    assert d.decision == Decision.ALLOW


def test_subprocess_allowed_for_coder(engine: PolicyEngine) -> None:
    m = ToolManifest(name="run_tests", requires_subprocess=True)
    d = engine.evaluate(_call(AgentRole.CODER), m)
    assert d.decision == Decision.ALLOW


def test_subprocess_denied_when_role_excluded(
    engine: PolicyEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pol, "_SUBPROCESS_OK", frozenset())
    m = ToolManifest(name="run_tests", requires_subprocess=True)
    d = engine.evaluate(_call(AgentRole.CODER), m)
    assert d.decision == Decision.DENY
    assert d.rule == "subprocess-role-allowlist"


def test_decision_carries_original_call(engine: PolicyEngine) -> None:
    call = _call(AgentRole.CODER, "noop")
    d = engine.evaluate(call, ToolManifest(name="noop"))
    assert isinstance(d, PolicyDecision)
    assert d.call == call


def test_default_rule_set_is_immutable(engine: PolicyEngine) -> None:
    assert isinstance(engine.rules, tuple)
    assert engine.rules == DEFAULT_RULES
