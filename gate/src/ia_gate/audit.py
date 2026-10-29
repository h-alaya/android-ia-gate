"""Audit log: one JSON line per tool call in `~/.ia-gate/audit.jsonl` (spec §6)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(
        self, tool: str, args: dict[str, Any], ok: bool, duration_ms: int, error: str | None = None
    ) -> None:
        """Append one entry (timestamp in ISO-8601 UTC); creates the parent directory if needed."""
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "tool": tool,
            "args": args,
            "ok": ok,
            "duration_ms": duration_ms,
            "error": error,
        }
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
