# Architecture - Textual Crew OS

Local-first multi-agent platform. Everything runs on loopback; the only
optional outbound path is the Tier-2 supervisor calling the Anthropic API.

## Layers

```
┌──────────────────────────────────────────────────────────────┐
│ UI            Dashboard (FastAPI, 127.0.0.1:8765)             │
│               REST + WebSocket, vanilla JS, ar/en i18n         │
├──────────────────────────────────────────────────────────────┤
│ Orchestration Delegator                                        │
│               ├─ SupervisorT1  (qwen2.5:3b router, rule-based) │
│               └─ SupervisorT2  (Claude Opus 4.7 → local 14B)   │
│               AgentRegistry · MessageBus (async pub/sub)       │
├──────────────────────────────────────────────────────────────┤
│ Agents        Coder · Sec-Defensive · Sec-Offensive · Auditor  │
│               (BaseAgent: tool registry + security pipeline)   │
├──────────────────────────────────────────────────────────────┤
│ LLM           OllamaClient · ModelManager (VRAM swap)          │
│               CircuitBreaker                                    │
├──────────────────────────────────────────────────────────────┤
│ Security      PolicyEngine · Sandbox (bubblewrap)              │
│               SubprocessSafe · RateLimiter · LabGate           │
│               AuditLogger (SHA-256 hash chain)                 │
├──────────────────────────────────────────────────────────────┤
│ Memory        SqliteStore (WAL) · JSONL append-only            │
├──────────────────────────────────────────────────────────────┤
│ Metrics       system (psutil) · gpu (nvidia-ml-py) · power     │
│               MetricsSampler (cache) · UsageTracker            │
└──────────────────────────────────────────────────────────────┘
```

## Request flow (`crew run`)

1. CLI builds the crew via the composition root (`cli/composition.py`).
2. `Delegator.delegate(task)`:
   - `SupervisorT1.route` classifies the task (deterministic, LLM-free)
     and decides target role + whether to escalate.
   - If escalated, `SupervisorT2.plan` produces a plan (Claude, or local
     `qwen3:14b` fallback when no API key / on Claude failure).
   - The target agent's `handle_task` runs.
3. Every tool call inside the agent passes the security pipeline:
   `rate limiter → policy engine → audit log → execute`.
4. The task's final state is persisted (`SqliteStore`) and published to
   the `MessageBus`; the dashboard WebSocket streams it live.

## VRAM management (8 GB budget)

`ModelManager` keeps exactly one model active. Before loading a new one
it explicitly unloads the current model (`keep_alive: 0`) and checks free
VRAM, serialised by an `asyncio.Lock`. Model loads are lazy: building the
agent objects never touches the GPU.

## Trust boundaries

| Boundary    | Inside                         | Outside                  |
|-------------|--------------------------------|--------------------------|
| Process     | Crew OS coroutines             | Other host processes     |
| Sandbox     | Coder subprocesses (bwrap)     | Supervisor process       |
| Network     | 127.0.0.1 (Ollama, dashboard)  | LAN / Internet           |
| Filesystem  | `$CREW_DATA_DIR`               | Rest of `$HOME`          |

See [`../SECURITY.md`](../SECURITY.md) for the threat model and the
controls that enforce each boundary.

## Key design decisions

- **Single enforcement point.** Routing to the offensive agent is not
  gated in the delegator; the policy engine blocks its tools when
  LAB_MODE is off (defense in depth, one place to reason about).
- **Deterministic escalation.** Tier-1 routing is rule-based so the
  expensive (paid / slow) Tier-2 escalation is predictable and auditable.
- **Tamper-evident audit.** The audit log is append-only JSONL with a
  SHA-256 hash chain; any edit/insert/delete is detectable via `verify()`.
- **Offline-first.** Claude is optional; without a key the whole system
  runs locally. The `anthropic` SDK is imported only when a key is set.
