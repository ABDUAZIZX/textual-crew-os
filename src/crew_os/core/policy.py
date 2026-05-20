"""Policy engine - evaluates every tool call before execution.

Design:

* Rules are pure functions of ``(ToolCall, ToolManifest, lab_mode_active)``.
* They return ``Decision.ALLOW`` / ``Decision.DENY`` to commit, or
  ``None`` to defer to the next rule.
* The engine evaluates rules in order; the first non-None result wins.
* If no rule matches, ``default_decision`` (closed-by-default: DENY)
  is returned.

This module has no side effects: callers are responsible for routing
the resulting :class:`PolicyDecision` to the audit log and either
executing or rejecting the tool call.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from crew_os.core.models import AgentRole, ToolCall, ToolManifest


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: Decision
    rule: str
    reason: str
    call: ToolCall


RulePredicate = Callable[[ToolCall, ToolManifest, bool], Decision | None]


@dataclass(frozen=True)
class Rule:
    name: str
    predicate: RulePredicate
    deny_reason: str = ""
    allow_reason: str = ""

    def evaluate(
        self,
        call: ToolCall,
        manifest: ToolManifest,
        lab_mode_active: bool,
    ) -> PolicyDecision | None:
        outcome = self.predicate(call, manifest, lab_mode_active)
        if outcome is None:
            return None
        reason = self.deny_reason if outcome == Decision.DENY else self.allow_reason
        return PolicyDecision(decision=outcome, rule=self.name, reason=reason, call=call)


# ─────────────────────────────── default rules ─────────────────────────


# Role allowlists for the deny-gates below.
_SUPERVISORS: frozenset[AgentRole] = frozenset({AgentRole.SUPERVISOR_T1, AgentRole.SUPERVISOR_T2})
_DANGEROUS_OK: frozenset[AgentRole] = frozenset(
    {
        AgentRole.SUPERVISOR_T1,
        AgentRole.SUPERVISOR_T2,
        AgentRole.SEC_DEFENSIVE,
        AgentRole.SEC_OFFENSIVE,
    }
)
_SUBPROCESS_OK: frozenset[AgentRole] = frozenset(
    {
        AgentRole.SUPERVISOR_T1,
        AgentRole.SUPERVISOR_T2,
        AgentRole.CODER,
        AgentRole.SEC_DEFENSIVE,
        AgentRole.SEC_OFFENSIVE,
        AgentRole.AUDITOR,
    }
)

# Model: a chain of deny-gates followed by a final permissive ALLOW. A tool
# call must clear every gate; whatever survives is permitted. This is closed
# unless a manifest's declared effects are explicitly allowed for the role.


def _r_lab_mode_required(
    _call: ToolCall, manifest: ToolManifest, lab_mode_active: bool
) -> Decision | None:
    if manifest.requires_lab_mode and not lab_mode_active:
        return Decision.DENY
    return None


def _r_offensive_agent_requires_lab(
    call: ToolCall, _manifest: ToolManifest, lab_mode_active: bool
) -> Decision | None:
    if call.agent_role == AgentRole.SEC_OFFENSIVE and not lab_mode_active:
        return Decision.DENY
    return None


def _r_network_only_supervisors(
    call: ToolCall, manifest: ToolManifest, _lab: bool
) -> Decision | None:
    if manifest.requires_network and call.agent_role not in _SUPERVISORS:
        return Decision.DENY
    return None


def _r_dangerous_tools_role(call: ToolCall, manifest: ToolManifest, _lab: bool) -> Decision | None:
    if manifest.dangerous and call.agent_role not in _DANGEROUS_OK:
        return Decision.DENY
    return None


def _r_subprocess_role(call: ToolCall, manifest: ToolManifest, _lab: bool) -> Decision | None:
    if manifest.requires_subprocess and call.agent_role not in _SUBPROCESS_OK:
        return Decision.DENY
    return None


def _r_allow_permitted(_call: ToolCall, _manifest: ToolManifest, _lab: bool) -> Decision | None:
    return Decision.ALLOW


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        name="lab-mode-required",
        predicate=_r_lab_mode_required,
        deny_reason="tool requires LAB_MODE but it is not active",
    ),
    Rule(
        name="offensive-agent-requires-lab",
        predicate=_r_offensive_agent_requires_lab,
        deny_reason="offensive agent invoked without LAB_MODE",
    ),
    Rule(
        name="network-egress-supervisor-only",
        predicate=_r_network_only_supervisors,
        deny_reason="only supervisor tiers may issue network-bound tool calls",
    ),
    Rule(
        name="dangerous-tool-role-allowlist",
        predicate=_r_dangerous_tools_role,
        deny_reason="dangerous tool restricted to supervisor or security roles",
    ),
    Rule(
        name="subprocess-role-allowlist",
        predicate=_r_subprocess_role,
        deny_reason="subprocess tool not permitted for this role",
    ),
    Rule(
        name="allow-permitted",
        predicate=_r_allow_permitted,
        allow_reason="cleared all policy gates",
    ),
)


# ─────────────────────────────── engine ────────────────────────────────


class PolicyEngine:
    """Stateless evaluator over a fixed rule list."""

    def __init__(
        self,
        rules: Sequence[Rule] = DEFAULT_RULES,
        *,
        default_decision: Decision = Decision.DENY,
    ) -> None:
        self._rules: tuple[Rule, ...] = tuple(rules)
        self._default = default_decision

    @property
    def rules(self) -> tuple[Rule, ...]:
        return self._rules

    def evaluate(
        self,
        call: ToolCall,
        manifest: ToolManifest,
        *,
        lab_mode_active: bool = False,
    ) -> PolicyDecision:
        for rule in self._rules:
            decision = rule.evaluate(call, manifest, lab_mode_active)
            if decision is not None:
                return decision
        return PolicyDecision(
            decision=self._default,
            rule="default",
            reason=(
                "closed-by-default: no rule matched"
                if self._default == Decision.DENY
                else "open-by-default"
            ),
            call=call,
        )
