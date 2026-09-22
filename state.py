"""Bounded transient tool observations and durable audit-attempt deduplication."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from mindroom.judgment.state import MAX_REQUEST_BYTES

MAX_TOOLS = 32
MAX_CAPTURES = 128
CAPTURE_TTL_SECONDS = 3600
CaptureKey = tuple[str, str, str, str, str]


@dataclass(slots=True)
class Capture:
    """Observations from a turn whose beginning this plugin actually witnessed."""

    started: float = field(default_factory=time.monotonic)
    tools: list[dict[str, object]] = field(default_factory=list)
    complete: bool = True
    size: int = 0

    def record(self, name: str, arguments: dict[str, object], result: object, *, failed: bool, blocked: bool) -> None:
        """Refuse oversized/unsupported evidence rather than silently truncate it."""
        if not self.complete:
            return
        item = {
            "name": name,
            "arguments": arguments,
            "result": result,
            "status": "blocked" if blocked else "failed" if failed else "succeeded",
        }
        try:
            serialized = json.dumps(item, ensure_ascii=False, allow_nan=False)
            size = len(serialized.encode())
        except (TypeError, ValueError, UnicodeError, RecursionError):
            self.complete = False
            return
        if len(self.tools) >= MAX_TOOLS or self.size + size > MAX_REQUEST_BYTES:
            self.complete = False
            self.tools.clear()
            return
        self.size += size
        self.tools.append(json.loads(serialized))


captures: OrderedDict[CaptureKey, Capture] = OrderedDict()


def prune() -> None:
    """Bound captures for turns that never reach a success/cancellation hook."""
    now = time.monotonic()
    for key, capture in list(captures.items()):
        if now - capture.started > CAPTURE_TTL_SECONDS:
            del captures[key]
    while len(captures) > MAX_CAPTURES:
        captures.popitem(last=False)


def claim_attempt(state_root: Path, agent: str, room: str, response_id: str) -> bool:
    """Reserve at most one audit attempt across replay/restart before any side effect."""
    state_root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state_root / "audit-attempts.sqlite3", timeout=1) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS attempts (agent TEXT, room TEXT, response TEXT, PRIMARY "
            "KEY(agent, room, response))"
        )
        cursor = db.execute("INSERT OR IGNORE INTO attempts VALUES (?, ?, ?)", (agent, room, response_id))
        return cursor.rowcount == 1
