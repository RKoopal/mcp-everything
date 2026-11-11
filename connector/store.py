"""Sessions and logs per site, readable only by the owner.

With a key, every file is encrypted (AES-256-GCM). The associated data binds a file to its
place (<folder>/<site>/<name>): a file copied to another user or site does not open.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path.home() / ".config" / "mcp-everything"
NONCE = 12


class Store:
    def __init__(self, site: str, root: Path = ROOT, key: bytes | None = None):
        root = Path(root)
        self.dir = root / site
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._aead = AESGCM(key) if key else None
        self._place = f"{root.name}/{site}/"

    def path(self, name: str) -> Path:
        return self.dir / name

    def _seal(self, name: str, raw: bytes) -> bytes:
        if not self._aead:
            return raw
        nonce = os.urandom(NONCE)
        return nonce + self._aead.encrypt(nonce, raw, (self._place + name).encode())

    def _unseal(self, name: str, data: bytes) -> bytes:
        return self._aead.decrypt(data[:NONCE], data[NONCE:], (self._place + name).encode()) if self._aead else data

    def read(self, name: str, default=None):
        path = self.path(name)
        return json.loads(self._unseal(name, path.read_bytes())) if path.exists() else default

    def write(self, name: str, data) -> None:
        """Atomic: a write that breaks off must not lose a rotating token. Every write has its
        own tmp file: two at the same time (two calls that both get a cookie) do not cut each other off."""
        raw = self._seal(name, json.dumps(data).encode())
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=f".{name}.", suffix=".tmp")  # 0600
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            os.replace(tmp, self.path(name))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def append(self, name: str, entry: dict) -> None:
        """Adds one line, with a key each encrypted on its own. For a log."""
        raw = json.dumps(entry).encode()
        line = base64.b64encode(self._seal(name, raw)) if self._aead else raw
        fd = os.open(self.path(name), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "ab") as f:
            f.write(line + b"\n")

    def lines(self, name: str) -> list[dict]:
        path = self.path(name)
        if not path.exists():
            return []
        return [json.loads(self._unseal(name, base64.b64decode(line)) if self._aead else line)
                for line in path.read_bytes().splitlines()]

    def delete(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)
