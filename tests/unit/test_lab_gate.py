"""Tests for ``crew_os.security.lab_gate`` - the third LAB_MODE gate."""

from __future__ import annotations

import io
from pathlib import Path

from crew_os.core.models import EventType, TraceContext
from crew_os.security.audit import AuditLogger
from crew_os.security.lab_gate import CONFIRMATION_PHRASE, request_lab_confirmation


def _audit(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "audit.jsonl")


def test_non_tty_input_denies_and_audits(tmp_path: Path) -> None:
    auditor = _audit(tmp_path)
    granted = request_lab_confirmation(
        scope="scan",
        target="127.0.0.1",
        auditor=auditor,
        trace=TraceContext.new_root(),
        input_stream=io.StringIO(CONFIRMATION_PHRASE + "\n"),
        output_stream=io.StringIO(),
        require_tty=True,
    )
    assert granted is False
    events = [r.event["type"] for r in auditor.iter_records()]
    assert events == [EventType.LAB_MODE_DENIED]


def test_wrong_phrase_denied(tmp_path: Path) -> None:
    auditor = _audit(tmp_path)
    granted = request_lab_confirmation(
        scope="scan",
        target="example.lab",
        auditor=auditor,
        trace=TraceContext.new_root(),
        input_stream=io.StringIO("yes\n"),
        output_stream=io.StringIO(),
        require_tty=False,
    )
    assert granted is False
    types = [r.event["type"] for r in auditor.iter_records()]
    assert types == [EventType.LAB_MODE_REQUESTED, EventType.LAB_MODE_DENIED]


def test_correct_phrase_grants(tmp_path: Path) -> None:
    auditor = _audit(tmp_path)
    out = io.StringIO()
    granted = request_lab_confirmation(
        scope="scan",
        target="example.lab",
        auditor=auditor,
        trace=TraceContext.new_root(),
        input_stream=io.StringIO(CONFIRMATION_PHRASE + "\n"),
        output_stream=out,
        require_tty=False,
    )
    assert granted is True
    types = [r.event["type"] for r in auditor.iter_records()]
    assert types == [EventType.LAB_MODE_REQUESTED, EventType.LAB_MODE_GRANTED]
    assert "LAB_MODE confirmation" in out.getvalue()
    assert "example.lab" in out.getvalue()


def test_eof_treated_as_deny(tmp_path: Path) -> None:
    auditor = _audit(tmp_path)
    granted = request_lab_confirmation(
        scope="scan",
        target="x",
        auditor=auditor,
        trace=TraceContext.new_root(),
        input_stream=io.StringIO(""),  # immediate EOF
        output_stream=io.StringIO(),
        require_tty=False,
    )
    assert granted is False
    types = [r.event["type"] for r in auditor.iter_records()]
    assert types == [EventType.LAB_MODE_REQUESTED, EventType.LAB_MODE_DENIED]


def test_trailing_newline_does_not_break_match(tmp_path: Path) -> None:
    auditor = _audit(tmp_path)
    granted = request_lab_confirmation(
        scope="x",
        target="y",
        auditor=auditor,
        trace=TraceContext.new_root(),
        input_stream=io.StringIO(CONFIRMATION_PHRASE + "\r\n"),
        output_stream=io.StringIO(),
        require_tty=False,
    )
    assert granted is True


def test_extra_whitespace_or_extra_chars_denied(tmp_path: Path) -> None:
    auditor = _audit(tmp_path)
    granted = request_lab_confirmation(
        scope="x",
        target="y",
        auditor=auditor,
        trace=TraceContext.new_root(),
        input_stream=io.StringIO(" " + CONFIRMATION_PHRASE + "\n"),
        output_stream=io.StringIO(),
        require_tty=False,
    )
    assert granted is False


def test_audit_chain_remains_valid_after_gate(tmp_path: Path) -> None:
    auditor = _audit(tmp_path)
    request_lab_confirmation(
        scope="s",
        target="t",
        auditor=auditor,
        trace=TraceContext.new_root(),
        input_stream=io.StringIO("nope\n"),
        output_stream=io.StringIO(),
        require_tty=False,
    )
    assert auditor.verify().valid is True
