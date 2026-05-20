"""Append-only JSONL writer with fsync and fcntl-based file locking.

Used by the audit log and by trace/event stores. Each call to
:meth:`JsonlWriter.append` writes a single JSON object on its own line,
flushes to disk, and (by default) issues ``fsync``. The fcntl exclusive
lock makes the writer safe under concurrent processes that share the
same target file.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any


class JsonlWriter:
    """Single-file append-only JSON Lines writer."""

    def __init__(self, path: Path, *, fsync_on_write: bool = True) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync_on_write
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(dict(record), ensure_ascii=False) + "\n"
        with self._lock, self._path.open("a", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(line)
                f.flush()
                if self._fsync:
                    os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def read_all(self) -> Iterator[dict[str, Any]]:
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    yield json.loads(stripped)

    def line_count(self) -> int:
        if not self._path.exists():
            return 0
        count = 0
        with self._path.open("rb") as f:
            for line in f:
                if line.strip():
                    count += 1
        return count
