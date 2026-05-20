"""Agent registry - look up agents by role or by declared capability."""

from __future__ import annotations

from crew_os.agents.base import BaseAgent
from crew_os.core.exceptions import AgentNotRegisteredError
from crew_os.core.models import AgentRole


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[AgentRole, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> None:
        if agent.role in self._agents:
            raise ValueError(f"agent for role {agent.role} already registered")
        self._agents[agent.role] = agent

    def get(self, role: AgentRole) -> BaseAgent | None:
        return self._agents.get(role)

    def require(self, role: AgentRole) -> BaseAgent:
        agent = self._agents.get(role)
        if agent is None:
            raise AgentNotRegisteredError(f"no agent registered for role {role}")
        return agent

    def roles(self) -> frozenset[AgentRole]:
        return frozenset(self._agents)

    def with_capability(self, tool_name: str) -> list[BaseAgent]:
        return [a for a in self._agents.values() if tool_name in a.capabilities]

    def __contains__(self, role: object) -> bool:
        return role in self._agents

    def __len__(self) -> int:
        return len(self._agents)
