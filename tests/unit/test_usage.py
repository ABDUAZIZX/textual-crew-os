"""Tests for ``crew_os.metrics.usage`` - token + credit accounting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from crew_os.metrics.usage import UsageTracker


class _Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t = self.t + timedelta(**kw)


def test_empty_report() -> None:
    r = UsageTracker().report()
    assert r.session.total_tokens == 0
    assert r.session.cost_usd == 0.0
    assert r.session.per_model == []
    assert r.week.total_tokens == 0


def test_local_model_is_free() -> None:
    tr = UsageTracker()
    tr.record("qwen2.5-coder:7b", input_tokens=1000, output_tokens=500)
    r = tr.report()
    assert r.session.total_tokens == 1500
    assert r.session.cost_usd == 0.0
    assert r.session.calls == 1


def test_priced_model_computes_cost() -> None:
    tr = UsageTracker(pricing={"claude-opus-4-7": (15.0, 75.0)})
    tr.record("claude-opus-4-7", input_tokens=1_000_000, output_tokens=1_000_000)
    r = tr.report()
    # 1M in * $15 + 1M out * $75 = $90
    assert r.session.cost_usd == 90.0


def test_explicit_cost_overrides_pricing() -> None:
    tr = UsageTracker()
    tr.record("claude-opus-4-7", input_tokens=10, output_tokens=10, cost_usd=2.5)
    assert tr.report().session.cost_usd == 2.5


def test_per_model_breakdown_grows_with_new_models() -> None:
    tr = UsageTracker()
    tr.record("qwen2.5:3b", output_tokens=100)
    r1 = tr.report()
    assert {m.model for m in r1.session.per_model} == {"qwen2.5:3b"}

    tr.record("llama3.1:8b", output_tokens=200)
    r2 = tr.report()
    assert {m.model for m in r2.session.per_model} == {"qwen2.5:3b", "llama3.1:8b"}


def test_week_window_excludes_old_events() -> None:
    clock = _Clock()
    tr = UsageTracker(now_fn=clock, week_days=7)
    tr.record("m", output_tokens=100)  # day 0
    clock.advance(days=10)
    tr.record("m", output_tokens=50)  # day 10
    r = tr.report()
    assert r.session.total_tokens == 150  # session keeps everything
    assert r.week.total_tokens == 50  # week only the recent one


def test_calls_counted() -> None:
    tr = UsageTracker()
    for _ in range(3):
        tr.record("m", output_tokens=10)
    assert tr.report().session.calls == 3
    assert tr.report().session.per_model[0].calls == 3
