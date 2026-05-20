"""Composition root: wire the whole crew from settings.

A single :func:`build_crew` constructs every component and returns a
:class:`Crew` bundle. Agent *objects* are built eagerly (they are cheap);
the models they use are loaded lazily by the :class:`ModelManager` on
first call, so this never touches the GPU by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crew_os.agents.auditor import AuditorAgent
from crew_os.agents.coder import CoderAgent
from crew_os.agents.sec_defensive import SecDefensiveAgent
from crew_os.agents.sec_offensive import SecOffensiveAgent
from crew_os.config import Settings, get_settings
from crew_os.core.policy import PolicyEngine
from crew_os.llm.model_manager import ModelManager
from crew_os.llm.ollama_client import OllamaClient
from crew_os.memory.chat_store import ChatStore
from crew_os.memory.sqlite_store import SqliteStore
from crew_os.metrics.sampler import MetricsSampler
from crew_os.metrics.usage import UsageTracker
from crew_os.orchestration.bus import MessageBus
from crew_os.orchestration.chat import ChatService
from crew_os.orchestration.delegator import Delegator
from crew_os.orchestration.registry import AgentRegistry
from crew_os.orchestration.supervisor_tier1 import SupervisorT1
from crew_os.orchestration.supervisor_tier2 import SupervisorT2, build_supervisor_t2
from crew_os.security.audit import AuditLogger
from crew_os.security.rate_limit import RateLimiter


@dataclass
class Crew:
    settings: Settings
    auditor: AuditLogger
    store: SqliteStore
    chat_store: ChatStore
    bus: MessageBus
    usage: UsageTracker
    ollama: OllamaClient
    model_manager: ModelManager
    registry: AgentRegistry
    chat_service: ChatService
    tier1: SupervisorT1
    tier2: SupervisorT2
    delegator: Delegator
    sampler: MetricsSampler

    async def aclose(self) -> None:
        await self.sampler.stop()
        await self.store.close()
        await self.chat_store.close()
        await self.ollama.aclose()


def _garak_python(settings: Settings) -> Path:
    if settings.garak_python is not None:
        return settings.garak_python
    return settings.data_dir / "garak-venv" / "bin" / "python"


async def build_crew(settings: Settings | None = None) -> Crew:
    settings = settings or get_settings()
    settings.ensure_dirs()
    data = settings.data_dir

    auditor = AuditLogger(data / "audit" / "audit.jsonl")
    store = SqliteStore(data / "memory" / "working.db")
    await store.connect()
    chat_store = ChatStore(data / "memory" / "chat_history.db")
    await chat_store.connect()
    bus = MessageBus()
    usage = UsageTracker()

    ollama = OllamaClient(settings.ollama_host, timeout=settings.ollama_timeout)
    model_manager = ModelManager(ollama, usage_tracker=usage)

    policy = PolicyEngine()
    rate_limiter = RateLimiter()
    # Chat send budget: ~20 burst, refilling 1/s (loopback, single user).
    rate_limiter.configure("chat", capacity=20, refill_per_sec=1.0)

    registry = AgentRegistry()
    registry.register(
        CoderAgent(
            workspace_root=data / "workspaces",
            policy=policy,
            rate_limiter=rate_limiter,
            auditor=auditor,
            model_manager=model_manager,
        )
    )
    registry.register(
        SecDefensiveAgent(
            policy=policy,
            rate_limiter=rate_limiter,
            auditor=auditor,
            read_root=data,
            model_manager=model_manager,
        )
    )
    registry.register(
        SecOffensiveAgent(
            policy=policy,
            rate_limiter=rate_limiter,
            auditor=auditor,
            model_manager=model_manager,
            lab_mode_active=settings.lab_mode,
        )
    )
    registry.register(
        AuditorAgent(
            policy=policy,
            rate_limiter=rate_limiter,
            auditor=auditor,
            garak_python=_garak_python(settings),
            report_dir=data / "reports",
        )
    )

    tier1 = SupervisorT1()
    tier2 = build_supervisor_t2(settings, model_manager=model_manager, usage_tracker=usage)
    delegator = Delegator(
        registry=registry,
        tier1=tier1,
        tier2=tier2,
        store=store,
        auditor=auditor,
        bus=bus,
    )
    sampler = MetricsSampler(interval=0.5)

    chat_service = ChatService(
        registry=registry,
        model_manager=model_manager,
        store=chat_store,
        auditor=auditor,
        rate_limiter=rate_limiter,
    )

    return Crew(
        settings=settings,
        auditor=auditor,
        store=store,
        chat_store=chat_store,
        bus=bus,
        usage=usage,
        ollama=ollama,
        model_manager=model_manager,
        registry=registry,
        chat_service=chat_service,
        tier1=tier1,
        tier2=tier2,
        delegator=delegator,
        sampler=sampler,
    )
