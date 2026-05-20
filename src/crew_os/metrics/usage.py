"""Usage tracker: token + credit accounting per model.

Aggregates two windows:

* **session** - everything recorded since the tracker was created.
* **week**    - everything within the last ``week_days`` (rolling).

A per-model breakdown is included in each window, so adding a new model
makes it appear automatically the first time it is used. Local models
cost nothing; priced models (Claude) use a per-million-token rate.

State is in-memory: weekly figures reset on restart. Durable accounting
is a later concern; this powers the live dashboard bar.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from crew_os.core.models import utcnow

# model -> (input USD per 1M tokens, output USD per 1M tokens).
# Local Ollama models are free; unknown models default to zero cost.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (15.0, 75.0),
}


@dataclass(frozen=True)
class _Event:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    when: datetime


class ModelUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float
    calls: int


class UsageWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_tokens: int
    cost_usd: float
    calls: int
    per_model: list[ModelUsage]


class UsageReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    session: UsageWindow
    week: UsageWindow
    currency: str = "USD"


class UsageTracker:
    def __init__(
        self,
        *,
        pricing: dict[str, tuple[float, float]] | None = None,
        now_fn: Callable[[], datetime] = utcnow,
        week_days: int = 7,
    ) -> None:
        self._events: list[_Event] = []
        self._pricing = pricing if pricing is not None else DEFAULT_PRICING
        self._now = now_fn
        self._week = timedelta(days=week_days)

    def _price(self, model: str, input_tokens: int, output_tokens: int) -> float:
        in_rate, out_rate = self._pricing.get(model, (0.0, 0.0))
        return input_tokens / 1_000_000 * in_rate + output_tokens / 1_000_000 * out_rate

    def record(
        self,
        model: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> None:
        cost = cost_usd if cost_usd is not None else self._price(model, input_tokens, output_tokens)
        self._events.append(
            _Event(
                model=model,
                input_tokens=max(0, input_tokens),
                output_tokens=max(0, output_tokens),
                cost_usd=cost,
                when=self._now(),
            )
        )

    @staticmethod
    def _aggregate(events: list[_Event]) -> UsageWindow:
        by_model: dict[str, dict[str, float]] = {}
        for e in events:
            acc = by_model.setdefault(
                e.model,
                {"input": 0, "output": 0, "cost": 0.0, "calls": 0},
            )
            acc["input"] += e.input_tokens
            acc["output"] += e.output_tokens
            acc["cost"] += e.cost_usd
            acc["calls"] += 1

        per_model = [
            ModelUsage(
                model=model,
                input_tokens=int(acc["input"]),
                output_tokens=int(acc["output"]),
                total_tokens=int(acc["input"] + acc["output"]),
                cost_usd=round(acc["cost"], 6),
                calls=int(acc["calls"]),
            )
            for model, acc in sorted(by_model.items())
        ]
        return UsageWindow(
            total_tokens=sum(m.total_tokens for m in per_model),
            cost_usd=round(sum(m.cost_usd for m in per_model), 6),
            calls=sum(m.calls for m in per_model),
            per_model=per_model,
        )

    def report(self) -> UsageReport:
        cutoff = self._now() - self._week
        week_events = [e for e in self._events if e.when >= cutoff]
        return UsageReport(
            session=self._aggregate(self._events),
            week=self._aggregate(week_events),
        )
