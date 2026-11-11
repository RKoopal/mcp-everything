"""Log of writes: every action has a key and happens only once."""
from __future__ import annotations

from datetime import UTC, datetime

from .store import Store

NAME = "ledger.jsonl"


class Ledger:
    def __init__(self, store: Store):
        self.store, self.path = store, store.path(NAME)

    def add(self, key: str, state: str, **fields) -> None:
        # ponytail: one process per site. More processes need a file lock.
        self.store.append(NAME, {"at": datetime.now(UTC).isoformat(), "key": key, "state": state, **fields})

    def get(self, key: str) -> dict | None:
        """The last line for this key."""
        return next((entry for entry in reversed(self.store.lines(NAME)) if entry["key"] == key), None)
