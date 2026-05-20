"""Exception hierarchy for Textual Crew OS.

All custom exceptions inherit from :class:`CrewOSError` so callers can
catch the entire family with one ``except`` clause.
"""

from __future__ import annotations


class CrewOSError(Exception):
    """Base for every exception raised by Crew OS code."""


class ConfigError(CrewOSError):
    """Configuration is missing, malformed, or violates an invariant."""


class PolicyViolation(CrewOSError):
    """A tool call was denied by the policy engine."""


class SubprocessError(CrewOSError):
    """A subprocess invocation failed validation or execution."""


class SandboxError(CrewOSError):
    """Sandbox setup failed (bwrap missing, bad mount spec, etc.)."""


class AuditChainBroken(CrewOSError):
    """The hash-chained audit log has been tampered with or corrupted."""


class RateLimitExceeded(CrewOSError):
    """Caller exceeded its per-key rate budget."""


class LabModeError(CrewOSError):
    """LAB_MODE activation refused or consent flow failed."""


class WorkingMemoryError(CrewOSError):
    """Persistence layer (SQLite / JSONL) raised an unexpected error."""


class OllamaError(CrewOSError):
    """The Ollama backend was unreachable or returned an error response."""


class CircuitOpenError(CrewOSError):
    """A circuit breaker is open and short-circuited the call."""


class InsufficientVRAMError(CrewOSError):
    """Not enough free GPU memory to load the requested model."""


class SupervisorError(CrewOSError):
    """No supervisor backend was available to handle an escalation."""


class AgentNotRegisteredError(CrewOSError):
    """A required agent role is not present in the registry."""
