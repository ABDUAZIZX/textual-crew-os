# Textual Crew OS

Local AI agents platform with a visual control dashboard, designed for
a single workstation with an 8 GB GPU. Runs entirely on loopback;
optional Claude API integration for high-complexity supervision.

> **Status:** functional (v1.1.0-local). Local agents, tiered supervision,
> live dashboard, hardened security layer, and a `crew` CLI.

## What It Does

- Orchestrates a team of local LLMs (Ollama) to deliver software-engineering
  and security-research tasks.
- Provides a tiered supervisor: a fast local router (qwen2.5:3b) plus an
  optional Claude Opus 4.7 escalation for complex planning.
- Ships a live dashboard at `127.0.0.1:8765` showing the agent network,
  task progress, GPU/CPU/RAM/power, and inter-agent traffic.
- Hard-isolates an offensive-research agent behind a multi-gate LAB_MODE.

## Quick Start

The installer is idempotent: it creates a venv, installs the package, wires
up Ollama (asking before it installs anything), and pulls the models named in
`.env`. Everything stays on loopback by default.

```bash
git clone https://github.com/ABDUAZIZX/textual-crew-os.git
cd textual-crew-os
./install.sh                 # add --dev for test/lint tooling
source .venv/bin/activate
crew dashboard               # open http://127.0.0.1:8765
```

If you prefer to do it by hand:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
python scripts/check_env.py        # verify Ollama, GPU, models
```

If `check_env.py` reports `Status: OK`, the host is ready.

### بدء سريع (عربي)

```bash
# 1) استنساخ المشروع
git clone https://github.com/ABDUAZIZX/textual-crew-os.git
cd textual-crew-os

# 2) تشغيل المُثبّت (يُنشئ venv، يُثبّت الحزم، يربط Ollama، يسحب الموديلات)
./install.sh

# 3) تفعيل البيئة وتشغيل لوحة التحكم
source .venv/bin/activate
crew dashboard               # افتح http://127.0.0.1:8765
```

المُثبّت **لا** يُثبّت Ollama دون أن يعرض لك الأمر ويطلب تأكيدك أولاً، ولا
يفتح المنفذ على الشبكة (loopback فقط). راجع `.env` قبل أول تشغيل لضبط
`CREW_ANTHROPIC_API_KEY` (اختياري) أو مسار البيانات.

## CLI

```bash
crew run "implement a function add(a, b)"   # delegate a task
crew status                                  # Ollama / GPU / agents
crew dashboard                               # serve + open the dashboard
crew audit-llm llama3.1:8b --probes encoding # run garak probes
crew logs --trace <id>                       # audit events for a trace
crew replay <trace_id>                       # replay a trace's events
```

## Docker (platform-only)

The image runs the platform and connects to an Ollama running on the host:

```bash
docker compose up --build      # then open http://127.0.0.1:8765
```

## Documentation

- [`docs/architecture.md`](docs/architecture.md) - layers and data flow
- [`docs/user_guide_en.md`](docs/user_guide_en.md) / [`docs/user_guide_ar.md`](docs/user_guide_ar.md)
- [`SECURITY.md`](SECURITY.md) - threat model + implemented controls

## Quality gates

`ruff` (lint + format), `mypy --strict`, `bandit`, `pip-audit`, and
`pytest` (300+ tests incl. a self-pentest suite) all run in pre-commit.

## Architecture (overview)

```
┌─────────────────────────────────────────────────┐
│  Dashboard (FastAPI + HTMX, 127.0.0.1:8765)     │
├─────────────────────────────────────────────────┤
│  Supervisor Tier 2 (optional: Claude Opus 4.7)  │
│  Supervisor Tier 1 (local: qwen2.5:3b router)   │
├─────────────────────────────────────────────────┤
│  Coder │ Sec-Defensive │ LAB_MODE │ Auditor     │
│  (ollama-served local models, one at a time)    │
├─────────────────────────────────────────────────┤
│  Policy Gate │ Sandbox (bubblewrap) │ Audit log │
├─────────────────────────────────────────────────┤
│  SQLite (working memory) │ JSONL (hash-chained) │
└─────────────────────────────────────────────────┘
```

See [`SECURITY.md`](SECURITY.md) for the threat model and LAB_MODE policy.

## Hardware Assumptions

- Linux (developed on Debian 13 Trixie).
- NVIDIA GPU with >= 6 GB free VRAM (tuned for RTX 2060 Super 8 GB).
- Python 3.11+.
- Ollama running on `127.0.0.1:11434`.

## License

MIT.
