"""Tests for ``crew_os.memory.jsonl_log``."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from crew_os.memory.jsonl_log import JsonlWriter


def test_append_and_readback(tmp_path: Path) -> None:
    w = JsonlWriter(tmp_path / "events.jsonl")
    w.append({"a": 1, "b": "two"})
    w.append({"a": 2, "b": "three"})
    records = list(w.read_all())
    assert records == [{"a": 1, "b": "two"}, {"a": 2, "b": "three"}]


def test_line_count_matches_appends(tmp_path: Path) -> None:
    w = JsonlWriter(tmp_path / "log.jsonl")
    assert w.line_count() == 0
    for i in range(5):
        w.append({"i": i})
    assert w.line_count() == 5


def test_read_all_on_missing_file_is_empty(tmp_path: Path) -> None:
    w = JsonlWriter(tmp_path / "absent.jsonl")
    assert list(w.read_all()) == []
    assert w.line_count() == 0


def test_preserves_unicode(tmp_path: Path) -> None:
    w = JsonlWriter(tmp_path / "u.jsonl")
    w.append({"msg": "مرحبا - بيت"})
    raw = (tmp_path / "u.jsonl").read_text(encoding="utf-8")
    assert "مرحبا" in raw
    assert json.loads(raw)["msg"] == "مرحبا - بيت"


def test_concurrent_writers_do_not_corrupt(tmp_path: Path) -> None:
    w = JsonlWriter(tmp_path / "c.jsonl")
    iters = 50

    def worker(tag: int) -> None:
        for i in range(iters):
            w.append({"tag": tag, "i": i})

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = (tmp_path / "c.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8 * iters
    for ln in lines:
        # Each line must be a complete, parseable JSON object.
        parsed = json.loads(ln)
        assert set(parsed.keys()) == {"tag", "i"}


def test_parent_directory_is_created(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c.jsonl"
    JsonlWriter(nested).append({"x": 1})
    assert nested.exists()
