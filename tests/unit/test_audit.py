"""Tests for ``crew_os.security.audit`` - hash-chained tamper detection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crew_os.core.exceptions import AuditChainBroken
from crew_os.core.models import Event, EventType, Severity, TraceContext
from crew_os.security.audit import GENESIS_HASH, AuditLogger


def _event(payload: dict[str, str] | None = None) -> Event:
    return Event(
        type=EventType.TOOL_CALL_REQUESTED,
        severity=Severity.INFO,
        trace=TraceContext.new_root(),
        payload=payload or {},
    )


# ─────────────────────────────── append / verify ───────────────────────


def test_empty_log_verifies(tmp_path: Path) -> None:
    log = AuditLogger(tmp_path / "audit.jsonl")
    result = log.verify()
    assert result.valid is True
    assert result.records == 0
    assert log.last_seq == -1
    assert log.last_hash == GENESIS_HASH


def test_single_append_chains_to_genesis(tmp_path: Path) -> None:
    log = AuditLogger(tmp_path / "audit.jsonl")
    rec = log.append(_event({"k": "v1"}))
    assert rec.seq == 0
    assert rec.prev_hash == GENESIS_HASH
    assert len(rec.hash) == 64
    assert log.verify().valid is True


def test_multiple_appends_chain_correctly(tmp_path: Path) -> None:
    log = AuditLogger(tmp_path / "audit.jsonl")
    prev = GENESIS_HASH
    for i in range(5):
        rec = log.append(_event({"i": str(i)}))
        assert rec.seq == i
        assert rec.prev_hash == prev
        prev = rec.hash
    assert log.verify().valid is True
    assert log.verify().records == 5


def test_reopen_continues_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    a = AuditLogger(path)
    a.append(_event())
    a.append(_event())

    b = AuditLogger(path)
    assert b.last_seq == 1
    assert b.last_hash == a.last_hash
    rec = b.append(_event())
    assert rec.seq == 2
    assert b.verify().valid is True


# ─────────────────────────────── tamper detection ──────────────────────


def test_detects_modified_event_payload(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLogger(path)
    log.append(_event({"who": "alice"}))
    log.append(_event({"who": "bob"}))

    lines = path.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["event"]["payload"]["who"] = "mallory"
    lines[0] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")

    result = log.verify()
    assert result.valid is False
    assert result.broken_at_line == 0
    assert result.error is not None
    assert "hash" in result.error


def test_detects_modified_hash_field(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLogger(path)
    log.append(_event())
    log.append(_event())

    lines = path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["hash"] = "0" * 64
    lines[1] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")

    assert log.verify().valid is False


def test_detects_deleted_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLogger(path)
    log.append(_event())
    log.append(_event())
    log.append(_event())

    lines = path.read_text().splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n")

    result = log.verify()
    assert result.valid is False
    assert result.error is not None
    assert ("seq" in result.error) or ("prev_hash" in result.error)


def test_detects_inserted_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLogger(path)
    log.append(_event())
    log.append(_event())

    fake = {
        "seq": 99,
        "ts": "2099-01-01T00:00:00+00:00",
        "prev_hash": "0" * 64,
        "hash": "f" * 64,
        "event": {},
    }
    with path.open("a") as f:
        f.write(json.dumps(fake) + "\n")

    assert log.verify().valid is False


def test_detects_corrupt_json_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLogger(path)
    log.append(_event())
    with path.open("a") as f:
        f.write("not-json{\n")
    result = log.verify()
    assert result.valid is False
    assert result.error is not None
    assert "json" in result.error


def test_verify_or_raise_succeeds_for_good_chain(tmp_path: Path) -> None:
    log = AuditLogger(tmp_path / "audit.jsonl")
    for _ in range(3):
        log.append(_event())
    assert log.verify_or_raise() == 3


def test_verify_or_raise_fails_for_broken_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLogger(path)
    log.append(_event())
    path.write_text("garbage\n")
    with pytest.raises(AuditChainBroken):
        log.verify_or_raise()


def test_iter_records_yields_in_order(tmp_path: Path) -> None:
    log = AuditLogger(tmp_path / "audit.jsonl")
    for i in range(4):
        log.append(_event({"i": str(i)}))
    seqs = [r.seq for r in log.iter_records()]
    assert seqs == [0, 1, 2, 3]
