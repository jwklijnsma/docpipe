"""
Content-hash cache: a source whose scanned files, model and prompt version are
unchanged since the last fully-passing run is skipped without any LLM call.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


class Cache:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        try:
            self.data: dict = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def fresh(self, name: str, digest: str, outputs: list[Path]) -> bool:
        entry = self.data.get(name)
        return bool(entry and entry.get("digest") == digest and all(p.exists() for p in outputs))

    def store(self, name: str, digest: str, revision: str | None) -> None:
        with self._lock:
            self.data[name] = {"digest": digest, "revision": revision,
                               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            atomic_write(self.path, json.dumps(self.data, indent=2))


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
