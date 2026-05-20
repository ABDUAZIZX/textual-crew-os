"""Tier-1 supervisor: deterministic local router.

Classifies a task into a category, scores its complexity on a 0-10 scale
with a pure rule-based scorer, and decides:

* which agent role should own it, and
* whether to escalate planning to Tier-2 (Claude / local 14B).

Everything here is deterministic and LLM-free, so routing is fully unit
testable and its escalation cost is predictable - escalation is what
spends money (or the slow local 14B), so we keep the decision auditable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from crew_os.core.models import AgentRole, Task


class TaskCategory(StrEnum):
    CODING = "coding"
    SECURITY_DEFENSIVE = "security_defensive"
    SECURITY_OFFENSIVE = "security_offensive"
    AUDIT = "audit"
    GENERAL = "general"


# Keyword sets per category. Order of _PRIORITY breaks ties.
_CATEGORY_KEYWORDS: dict[TaskCategory, tuple[str, ...]] = {
    TaskCategory.SECURITY_OFFENSIVE: (
        "exploit",
        "offensive",
        "penetration",
        "pentest",
        "fuzz",
        "payload",
        "reverse shell",
        "privilege escalation",
    ),
    TaskCategory.AUDIT: (
        "garak",
        "probe",
        "red team",
        "evaluate model",
        "llm audit",
        "jailbreak test",
    ),
    TaskCategory.SECURITY_DEFENSIVE: (
        "harden",
        "defend",
        "secure",
        "vulnerability",
        "cve",
        "threat model",
        "mitigation",
    ),
    TaskCategory.CODING: (
        "code",
        "implement",
        "function",
        "refactor",
        "bug",
        "unit test",
        "api",
        "class",
        "module",
        "endpoint",
    ),
}

_PRIORITY: tuple[TaskCategory, ...] = (
    TaskCategory.SECURITY_OFFENSIVE,
    TaskCategory.AUDIT,
    TaskCategory.SECURITY_DEFENSIVE,
    TaskCategory.CODING,
)

_ROLE_BY_CATEGORY: dict[TaskCategory, AgentRole] = {
    TaskCategory.CODING: AgentRole.CODER,
    TaskCategory.SECURITY_DEFENSIVE: AgentRole.SEC_DEFENSIVE,
    TaskCategory.SECURITY_OFFENSIVE: AgentRole.SEC_OFFENSIVE,
    TaskCategory.AUDIT: AgentRole.AUDITOR,
    TaskCategory.GENERAL: AgentRole.CODER,
}

_CATEGORY_BASE_COMPLEXITY: dict[TaskCategory, int] = {
    TaskCategory.SECURITY_OFFENSIVE: 3,
    TaskCategory.AUDIT: 2,
    TaskCategory.SECURITY_DEFENSIVE: 1,
    TaskCategory.CODING: 0,
    TaskCategory.GENERAL: 0,
}

_COMPLEX_MARKERS: tuple[str, ...] = (
    "architecture",
    "design",
    "multi-step",
    "end-to-end",
    "entire",
    "complex",
    "distributed",
    "migrate",
    "from scratch",
)

MAX_COMPLEXITY = 10

ScoreFn = Callable[[Task, TaskCategory], int]


@dataclass(frozen=True)
class RoutingDecision:
    target_role: AgentRole
    category: TaskCategory
    complexity: int
    escalate_to_t2: bool
    reason: str


class SupervisorT1:
    def __init__(
        self,
        *,
        escalation_threshold: int = 7,
        score_fn: ScoreFn | None = None,
    ) -> None:
        if not 0 <= escalation_threshold <= MAX_COMPLEXITY:
            raise ValueError(f"escalation_threshold must be in [0, {MAX_COMPLEXITY}]")
        self._threshold = escalation_threshold
        self._score_fn = score_fn or self.score_complexity

    @property
    def escalation_threshold(self) -> int:
        return self._threshold

    def classify(self, task: Task) -> TaskCategory:
        text = task.description.lower()
        hits: dict[TaskCategory, int] = {}
        for category, keywords in _CATEGORY_KEYWORDS.items():
            count = sum(1 for kw in keywords if kw in text)
            if count:
                hits[category] = count
        if not hits:
            return TaskCategory.GENERAL
        best = max(hits.values())
        for category in _PRIORITY:
            if hits.get(category, 0) == best:
                return category
        return TaskCategory.GENERAL

    def score_complexity(self, task: Task, category: TaskCategory) -> int:
        text = task.description.lower()
        score = _CATEGORY_BASE_COMPLEXITY[category]
        score += min(4, len(task.description.split()) // 40)
        score += sum(1 for marker in _COMPLEX_MARKERS if marker in text)
        score += text.count(" then ")
        return min(MAX_COMPLEXITY, score)

    def route(self, task: Task) -> RoutingDecision:
        category = self.classify(task)
        complexity = self._score_fn(task, category)
        complexity = max(0, min(MAX_COMPLEXITY, complexity))
        escalate = complexity >= self._threshold
        return RoutingDecision(
            target_role=_ROLE_BY_CATEGORY[category],
            category=category,
            complexity=complexity,
            escalate_to_t2=escalate,
            reason=(
                f"category={category.value} complexity={complexity} "
                f"threshold={self._threshold} -> "
                f"{'escalate' if escalate else 'local'}"
            ),
        )
