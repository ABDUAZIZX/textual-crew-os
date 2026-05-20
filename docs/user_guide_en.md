# User Guide (English)

Textual Crew OS - a local AI agent platform with a live dashboard.

## Requirements

- Linux (developed on Debian 13), Python 3.11+
- Ollama running on `127.0.0.1:11434`
- NVIDIA GPU with >= 6 GB free VRAM (tuned for RTX 2060 Super 8 GB)
- `bubblewrap` for sandboxed code execution: `sudo apt install bubblewrap`

## Install

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # adjust if needed
python scripts/check_env.py   # verify Ollama, GPU, models
scripts/verify_models.sh      # verify required models are pulled
```

`check_env.py` should report `Status: OK`.

## Configuration (`.env`)

All variables use the `CREW_` prefix:

| Variable | Default | Notes |
|---|---|---|
| `CREW_DATA_DIR` | `~/.local/share/textual-crew-os` | SQLite, audit log, traces |
| `CREW_OLLAMA_HOST` | `http://127.0.0.1:11434` | must be loopback |
| `CREW_ANTHROPIC_API_KEY` | (unset) | optional; enables Claude Tier-2 |
| `CREW_WEB_HOST` / `CREW_WEB_PORT` | `127.0.0.1` / `8765` | host must be loopback |
| `CREW_GARAK_PYTHON` | `<data>/garak-venv/bin/python` | isolated garak interpreter |
| `CREW_LAB_MODE` | `0` | offensive agent gate (see below) |

## Commands

```bash
crew run "implement a function add(a, b)"   # delegate a task
crew status                                  # Ollama / GPU / agents
crew dashboard                               # serve + open the dashboard
crew audit-llm llama3.1:8b --probes encoding # run garak probes
crew logs --trace <id>                       # audit events for a trace
crew replay <trace_id>                       # replay a trace's events
```

## Dashboard

`crew dashboard` serves `http://127.0.0.1:8765` and opens your browser.
Top bar has a language toggle (English / العربية) and a usage-credits bar
(current session + current week, per model). All text is selectable.

## LAB_MODE (offensive agent)

Disabled by default. Enabling it is a three-gate process:

1. `CREW_LAB_MODE=1` in the environment.
2. A consent token, recent (< 7 days), `chmod 600`:
   ```bash
   mkdir -p ~/.crew && date -u +%s > ~/.crew/lab_consent.token
   chmod 600 ~/.crew/lab_consent.token
   ```
3. An interactive confirmation the first time an offensive task runs.

LAB_MODE is intended for authorized testing of systems you own (CTF,
labs, sanctioned pentests). Every activation is recorded in the audit log.

## garak (Auditor)

Set up the isolated venv once:

```bash
scripts/setup_garak_venv.sh
```

Then: `crew audit-llm <model> --probes <probe1,probe2>`.

## Verifying integrity

The audit log is hash-chained. To check it programmatically:

```python
from crew_os.security.audit import AuditLogger
print(AuditLogger("<data>/audit/audit.jsonl").verify())
```
