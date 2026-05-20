"""Tamper-evident audit log.

Records are written as JSON Lines. Each line contains a SHA-256 hash that
binds it to the previous line (genesis = 64 zeros). Recomputing the
chain from disk detects any insertion, deletion, or modification.

The on-disk record schema is::

    {
      "seq":       int,
      "ts":        ISO-8601 UTC,
      "prev_hash": 64-hex,
      "hash":      64-hex,
      "event":     <canonical-json of Event>
    }

The hash payload is the literal string::

    f"{prev_hash}|{seq}|{ts}|{canonical_event_json}"

where ``canonical_event_json`` is ``json.dumps(event, sort_keys=True)``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from crew_os.core.exceptions import AuditChainBroken
from crew_os.core.models import Event

GENESIS_HASH: str = "0" * 64


class AuditRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int
    ts: str
    prev_hash: str
    hash: str
    event: dict[str, Any]


class VerifyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    records: int
    valid: bool
    broken_at_line: int | None = None
    error: str | None = None


def _canonical_event_json(event_payload: dict[str, Any]) -> str:
    return json.dumps(event_payload, sort_keys=True, ensure_ascii=False)


def _compute_hash(prev_hash: str, seq: int, ts: str, event_json: str) -> str:
    payload = f"{prev_hash}|{seq}|{ts}|{event_json}".encode()
    return hashlib.sha256(payload).hexdigest()


class AuditLogger:
    """Append-only, hash-chained event log."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_seq, self._last_hash = self._read_tail()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def last_hash(self) -> str:
        return self._last_hash

    def _read_tail(self) -> tuple[int, str]:
        if not self._path.exists():
            return -1, GENESIS_HASH
        last: dict[str, Any] | None = None
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    last = json.loads(stripped)
        if last is None:
            return -1, GENESIS_HASH
        return int(last["seq"]), str(last["hash"])

    def append(self, event: Event) -> AuditRecord:
        with self._lock:
            seq = self._last_seq + 1
            ts = datetime.now(UTC).isoformat()
            event_payload = event.model_dump(mode="json")
            event_json = _canonical_event_json(event_payload)
            h = _compute_hash(self._last_hash, seq, ts, event_json)
            record_dict = {
                "seq": seq,
                "ts": ts,
                "prev_hash": self._last_hash,
                "hash": h,
                "event": event_payload,
            }
            line = json.dumps(record_dict, ensure_ascii=False) + "\n"

            with self._path.open("a", encoding="utf-8") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            self._last_seq = seq
            self._last_hash = h
            return AuditRecord(**record_dict)

    def iter_records(self) -> Iterator[AuditRecord]:
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    yield AuditRecord(**json.loads(stripped))

    def verify(self) -> VerifyResult:
        prev_hash = GENESIS_HASH
        expected_seq = 0
        count = 0
        if not self._path.exists():
            return VerifyResult(records=0, valid=True)
        with self._path.open("r", encoding="utf-8") as f:
            for line_no, raw in enumerate(f):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    return VerifyResult(
                        records=count,
                        valid=False,
                        broken_at_line=line_no,
                        error=f"invalid json: {exc}",
                    )
                if rec.get("seq") != expected_seq:
                    return VerifyResult(
                        records=count,
                        valid=False,
                        broken_at_line=line_no,
                        error=f"seq gap: expected {expected_seq}, got {rec.get('seq')}",
                    )
                if rec.get("prev_hash") != prev_hash:
                    return VerifyResult(
                        records=count,
                        valid=False,
                        broken_at_line=line_no,
                        error="prev_hash mismatch",
                    )
                event_json = _canonical_event_json(rec.get("event", {}))
                computed = _compute_hash(prev_hash, expected_seq, rec["ts"], event_json)
                if computed != rec.get("hash"):
                    return VerifyResult(
                        records=count,
                        valid=False,
                        broken_at_line=line_no,
                        error="hash mismatch",
                    )
                prev_hash = computed
                expected_seq += 1
                count += 1
        return VerifyResult(records=count, valid=True)

    def verify_or_raise(self) -> int:
        result = self.verify()
        if not result.valid:
            raise AuditChainBroken(
                f"audit chain broken at line {result.broken_at_line}: {result.error}"
            )
        return result.records
