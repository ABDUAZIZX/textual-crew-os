"""Third LAB_MODE gate: interactive operator confirmation.

The first two gates live in :mod:`crew_os.config` (env var + consent
token). This module gates the *first* offensive task in a session by
forcing the operator to type an exact passphrase on a real TTY.

Each interaction emits two audit events:

* ``LAB_MODE_REQUESTED`` - prompt was shown.
* ``LAB_MODE_GRANTED`` / ``LAB_MODE_DENIED`` - operator's decision.

This makes the gate self-auditing even if the surrounding agent code is
buggy.
"""

from __future__ import annotations

import sys
from typing import TextIO

from crew_os.core.models import Event, EventType, Severity, TraceContext
from crew_os.security.audit import AuditLogger

CONFIRMATION_PHRASE: str = "ACTIVATE LAB_MODE"


def request_lab_confirmation(
    *,
    scope: str,
    target: str,
    auditor: AuditLogger,
    trace: TraceContext,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    require_tty: bool = True,
    expected_phrase: str = CONFIRMATION_PHRASE,
) -> bool:
    """Ask the operator to confirm LAB_MODE for the given scope and target.

    Returns ``True`` only if the operator types ``expected_phrase`` on
    stdin verbatim. Any other response, EOF, or non-TTY stdin denies.

    Args:
        scope: short human description of the operation
            (e.g. ``"network scan"``).
        target: the specific resource being acted on (URL, host, file).
        auditor: the audit log to append request/grant/deny events to.
        trace: trace context for cross-event correlation.
        input_stream: defaults to :data:`sys.stdin`. Injected for tests.
        output_stream: defaults to :data:`sys.stderr`. Injected for tests.
        require_tty: when True (the default), refuse unless the input
            stream is a real TTY. Pass ``False`` from tests.
        expected_phrase: passphrase the operator must type verbatim.
    """

    in_stream = input_stream if input_stream is not None else sys.stdin
    out_stream = output_stream if output_stream is not None else sys.stderr

    if require_tty:
        is_tty = getattr(in_stream, "isatty", lambda: False)()
        if not is_tty:
            auditor.append(
                Event(
                    type=EventType.LAB_MODE_DENIED,
                    severity=Severity.WARNING,
                    trace=trace,
                    payload={
                        "scope": scope,
                        "target": target,
                        "reason": "stdin is not a TTY",
                    },
                )
            )
            return False

    auditor.append(
        Event(
            type=EventType.LAB_MODE_REQUESTED,
            severity=Severity.WARNING,
            trace=trace,
            payload={"scope": scope, "target": target},
        )
    )

    banner = (
        "\n"
        + "=" * 60
        + "\n"
        + "LAB_MODE confirmation requested.\n"
        + f"  scope:  {scope}\n"
        + f"  target: {target}\n"
        + f"Type exactly: {expected_phrase!r}\n"
        + "Anything else, including EOF, aborts.\n"
        + "=" * 60
        + "\n> "
    )
    try:
        out_stream.write(banner)
        out_stream.flush()
    except OSError:
        pass

    try:
        response = in_stream.readline()
    except (OSError, EOFError):
        response = ""

    granted = response.rstrip("\r\n") == expected_phrase
    auditor.append(
        Event(
            type=EventType.LAB_MODE_GRANTED if granted else EventType.LAB_MODE_DENIED,
            severity=Severity.CRITICAL if granted else Severity.WARNING,
            trace=trace,
            payload={
                "scope": scope,
                "target": target,
                "response_matched": granted,
            },
        )
    )
    return granted
