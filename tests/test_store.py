"""The vault: encrypted, only for the owner, and bound to its place."""
from __future__ import annotations

import os
import shutil
import stat

import pytest
from cryptography.exceptions import InvalidTag

from connector.store import Store

KEY = os.urandom(32)


def test_files_are_private_and_leave_no_temp_file(tmp_path):
    store = Store("site", tmp_path)
    store.write("session.json", {"a": 1})
    assert stat.S_IMODE(store.path("session.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(store.dir.stat().st_mode) == 0o700
    assert [p.name for p in store.dir.iterdir()] == ["session.json"]


def test_with_a_key_nothing_is_readable_on_disk(tmp_path):
    store = Store("site", tmp_path, KEY)
    store.write("session.json", {"cookies": {"sid": "geheim"}})
    assert b"geheim" not in store.path("session.json").read_bytes()
    assert store.read("session.json") == {"cookies": {"sid": "geheim"}}


def test_a_file_copied_to_another_user_does_not_open(tmp_path):
    mine, theirs = Store("site", tmp_path / "anna", KEY), Store("site", tmp_path / "bob", KEY)
    mine.write("session.json", {"sid": "van anna"})
    shutil.copy(mine.path("session.json"), theirs.path("session.json"))
    with pytest.raises(InvalidTag):
        theirs.read("session.json")


def test_another_key_does_not_open(tmp_path):
    Store("site", tmp_path, KEY).write("tokens.json", {"t": 1})
    with pytest.raises(InvalidTag):
        Store("site", tmp_path, os.urandom(32)).read("tokens.json")


def test_a_log_is_encrypted_per_line_and_private(tmp_path):
    store = Store("site", tmp_path, KEY)
    store.append("ledger.jsonl", {"key": "k1", "result": "geheim"})
    store.append("ledger.jsonl", {"key": "k2"})
    assert b"geheim" not in store.path("ledger.jsonl").read_bytes()
    assert stat.S_IMODE(store.path("ledger.jsonl").stat().st_mode) == 0o600
    assert store.lines("ledger.jsonl") == [{"key": "k1", "result": "geheim"}, {"key": "k2"}]
    assert Store("site", tmp_path / "ander", KEY).lines("ledger.jsonl") == []


def test_two_writes_at_once_do_not_break_each_other(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = Store("site", tmp_path, KEY)
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda n: store.write("session.json", {"n": n, "pad": "x" * 100_000}), range(64)))
    assert store.read("session.json")["n"] in range(64)
    assert [p.name for p in store.dir.iterdir()] == ["session.json"]
