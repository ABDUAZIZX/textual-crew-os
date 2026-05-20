# Security Model - Textual Crew OS

This document describes the threat model and security guarantees of
Textual Crew OS. It is reviewed at every stage of development.

نموذج التهديد لمنصة وكلاء AI المحلية. يُراجع في كل مرحلة تطوير.

---

## Trust Boundaries

| Boundary | Inside | Outside |
|---|---|---|
| Process | Crew OS Python process | All other processes on host |
| Sandbox | Each agent subprocess (bubblewrap) | Crew OS supervisor |
| Network | `127.0.0.1` only (Ollama, Dashboard) | Public network, LAN |
| Filesystem | `$CREW_DATA_DIR` only | Rest of `$HOME` |

---

## Assets

1. **User code and secrets** - never sent to remote services without
   explicit opt-in via `CREW_ANTHROPIC_API_KEY`.
2. **Audit log** - append-only, SHA-256 hash-chained. Tampering detectable.
3. **LAB_MODE consent token** - controls activation of offensive agent.
4. **Anthropic API key** - optional, lives only in `.env` (gitignored).

---

## Adversary Models

### A1. Network attacker (LAN / Internet)
- **Goal:** abuse the local GPU, exfiltrate data via the dashboard.
- **Mitigations:**
  - Ollama bind enforced to loopback (verified at startup by
    `scripts/check_env.py`).
  - Dashboard bind enforced to loopback in `crew_os.config`.
  - No outbound network traffic from agent subprocesses; only the
    Supervisor may call Anthropic.

### A2. Compromised LLM output (prompt injection)
- **Goal:** convince an agent to exfiltrate data or run dangerous
  commands.
- **Mitigations:**
  - Policy Gate (Stage 1) validates every tool call before execution.
  - Subprocesses sandboxed via bubblewrap (read-only `/`, scoped writes).
  - No `shell=True`. All user-derived arguments pass through
    `shlex.quote` or an allowlist.
  - Agents declare capabilities in a manifest; calls outside the manifest
    are rejected.

### A3. Stolen LAB_MODE consent token
- **Goal:** activate the offensive agent without operator awareness.
- **Mitigations:**
  - Token must be `chmod 600` (loose permissions rejected at startup).
  - Token freshness window: 7 days; must be re-issued by the operator.
  - Third gate: interactive CLI confirm before the first offensive task.
  - Every LAB_MODE invocation appended to the audit log with scope.

### A4. Supply-chain (malicious dependency)
- **Mitigations:**
  - `pip-audit` runs in pre-commit and CI.
  - `bandit` scans source for unsafe patterns.
  - Minimal pinned direct dependencies (see `pyproject.toml`).
  - `garak` runs in an isolated venv via subprocess - its transitive
    deps never enter the main environment.

---

## Out of Scope

- Adversary with root on the host.
- Hardware-level attacks (Spectre-class, GPU side channels).
- Anthropic API itself (trusted vendor boundary, off by default).
- Physical access to the machine.

---

## LAB_MODE Policy

LAB_MODE enables an offensive-tuned LLM (e.g. WhiteRabbitNeo). Intended
uses:

- Defensive research on systems the operator owns.
- CTF challenges, lab environments, authorized penetration tests.

Prohibited uses:

- Attacks against third-party systems without written authorization.
- Generating attack tooling for distribution.

Activation requires **all three** of:

1. `CREW_LAB_MODE=1` in environment.
2. `~/.crew/lab_consent.token` exists, `chmod 600`, recent (< 7 days).
3. Interactive CLI confirm at the first offensive task each session.

The audit log records every LAB_MODE invocation with target scope.

---

## Implemented Controls (verified)

Each adversary maps to concrete code and a self-pentest assertion in
`tests/security/test_self_pentest.py` (the suite fails if a control
regresses).

| Adversary | Control | Code | Pentest assertion |
|---|---|---|---|
| A1 public bind | loopback-only validation | `config.Settings` validators | `test_attack_public_{web,ollama}_bind_blocked` |
| A2 path traversal | workspace confinement | `agents/coder._resolve` | `test_attack_path_traversal_blocked` |
| A2 command injection | argv-only, no shell | `security/subprocess_safe` | `test_attack_shell_injection_is_literal`, `test_attack_nul_byte_argv_rejected` |
| A2 binary abuse | allowlist | `subprocess_safe.run_safe` | `test_attack_binary_allowlist_enforced` |
| A2 garak injection | strict input regex | `agents/auditor.build_garak_argv` | `test_attack_garak_probe_injection_rejected` |
| A2 dangerous tool | role allowlist | `core/policy` | `test_attack_dangerous_tool_for_coder_blocked` |
| A3 LAB bypass | policy + agent gate | `policy`, `agents/sec_offensive` | `test_attack_offensive_tool_without_lab_blocked` |
| A3 token forge | perms + freshness | `config._verify_lab_consent` | `test_attack_{world_readable,stale}_lab_token_rejected` |
| A2 audit tamper | SHA-256 hash chain | `security/audit` | `test_attack_audit_tamper_detected` |
| DoS | token-bucket limits | `security/rate_limit` | `test_attack_rate_limit_enforced` |
| egress | sandbox unshares net | `security/sandbox` | `test_attack_sandbox_network_egress_blocked` |
| egress (defense-in-depth) | no agent network tools | agent manifests | `test_no_executor_agent_exposes_network_tool` |

## Dependency Auditing

`pip-audit` runs in pre-commit and CI. Last run: **no known
vulnerabilities** across direct + transitive dependencies.

## Residual Risks (accepted)

- **garak subprocess is not sandboxed.** It needs loopback access to
  Ollama and may fetch probe data, so it runs via the isolated venv with
  a validated argv (no shell) rather than under bubblewrap.
- **Per-agent CPU/RAM is not measured.** Agents are coroutines in one
  process; the dashboard shows system-wide metrics, not per-agent.
- **Weekly usage figures reset on restart** (in-memory `UsageTracker`).
- **In-container sandboxing** may require user-namespace support; see
  `docker-compose.yml`.

## Reporting Issues

Security issues: open a private security advisory on the repository
or email the maintainer. Do not file public issues for vulnerabilities.
