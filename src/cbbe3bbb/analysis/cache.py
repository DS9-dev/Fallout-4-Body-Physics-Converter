"""SQLite cache of per-project analysis results keyed by file fingerprints."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

__all__ = ["AnalysisCache"]


class AnalysisCache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL)")
        self._db.commit()

    def get(self, key: str, fingerprint: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT fingerprint, payload FROM results WHERE key = ?", (key,)).fetchone()
        if row and row[0] == fingerprint:
            try:
                return json.loads(row[1])
            except json.JSONDecodeError:
                return None
        return None

    def put(self, key: str, fingerprint: str, payload: dict) -> None:
        data = json.dumps(payload)
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO results (key, fingerprint, payload) VALUES (?, ?, ?)", (key, fingerprint, data))

    def commit(self) -> None:
        with self._lock:
            self._db.commit()

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM results")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.commit()
            self._db.close()
