"""Accounts, invites and recordings in SQLite."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import time

import pytest

from connector.users import SignupError, Users, main


def test_users_store_only_a_hash_and_rotate_tokens(tmp_path):
    users = Users(tmp_path)
    first = users.add("anna")
    second = users.add("anna")
    assert users.by_token(first) is None and users.by_token(second)["name"] == "anna"
    assert len(users.all()) == 1 and second.encode() not in (tmp_path / "connector.db").read_bytes()
    assert stat.S_IMODE((tmp_path / "connector.db").stat().st_mode) == 0o600
    assert users.by_token("usc_fout") is None and users.by_token("") is None


def test_accounts_from_users_json_move_over_with_their_tokens(tmp_path):
    token = "usc_" + "t" * 43  # as until 2026-11-05: a list in users.json, with the hash of the token
    (tmp_path / "users.json").write_text(json.dumps([{"id": "0123abcd", "name": "reimer", "created": 1,
                                                       "token": hashlib.sha256(token.encode()).hexdigest()}]))
    users = Users(tmp_path)
    assert users.by_token(token) == {"id": "0123abcd", "name": "reimer", "invite": None}
    assert not (tmp_path / "users.json").exists() and (tmp_path / "users.json.imported").exists()
    assert not users.invited("0123abcd")
    assert Users(tmp_path).by_token(token)  # a second start imports nothing anymore


def test_an_invite_works_as_often_as_it_says(tmp_path):
    users = Users(tmp_path)
    code = users.invite(uses=2, note="demo")
    anna = users.redeem(code, "Anna ", max_users=10)
    assert users.by_token(anna)["name"] == "anna" and users.invited(users.by_token(anna)["id"])
    with pytest.raises(SignupError, match="taken"):
        users.redeem(code, "anna", max_users=10)
    with pytest.raises(SignupError, match="Pick a name"):
        users.redeem(code, "a", max_users=10)
    users.redeem(code, "bob", max_users=10)
    assert not users.open_invite(code, max_users=10)
    with pytest.raises(SignupError, match="no longer works"):
        users.redeem(code, "carla", max_users=10)
    with pytest.raises(SignupError, match="no longer works"):
        users.redeem("verzonnen", "carla", max_users=10)
    assert [(i["used"], i["uses"], i["note"]) for i in users.invites()] == [(2, 2, "demo")]
    assert code not in (tmp_path / "connector.db").read_text(errors="ignore")


def test_no_invite_works_once_the_server_is_full(tmp_path):
    users = Users(tmp_path)
    users.add("reimer")
    code = users.invite(uses=5)
    users.redeem(code, "anna", max_users=2)
    assert not users.open_invite(code, max_users=2)
    with pytest.raises(SignupError, match="no longer works"):
        users.redeem(code, "bob", max_users=2)


def test_recordings_are_counted_per_user(tmp_path):
    users = Users(tmp_path)
    users.add("reimer")
    anna = users.by_token(users.redeem(users.invite(), "anna", max_users=10))
    reimer = next(u for u in users.all() if u["name"] == "reimer")
    users.record("j1", anna["id"], "a.nl")
    users.asked("j1")
    users.recorded("j1", "a", new_site=True, added=["a_zoek"])
    users.record("j2", anna["id"], "a.nl")  # without API requests: Claude was not called
    users.recorded("j2", error="Geen API-verzoeken")
    users.record("j3", reimer["id"], "b.nl")
    users.asked("j3")
    hour_ago = time.time() - 3600
    assert users.recordings_since(hour_ago, anna["id"]) == 1
    assert users.recordings_since(hour_ago, reimer["id"]) == 1
    assert users.recordings_since(time.time() + 60, anna["id"]) == 0


def test_a_password_is_a_slow_hash_and_only_the_right_one_works(tmp_path):
    users = Users(tmp_path)
    users.add("anna")
    assert users.by_password("anna", "lang-genoeg-wachtwoord") is None  # from `add`: no password yet
    assert users.set_password("Anna", "lang-genoeg-wachtwoord") and not users.set_password("niemand", "lang-genoeg-x")
    assert users.by_password(" ANNA", "lang-genoeg-wachtwoord")["name"] == "anna"
    assert users.by_password("anna", "lang-genoeg-wachtwoorD") is None
    with pytest.raises(SignupError, match="at least 10"):
        users.set_password("anna", "kort")
    stored = sqlite3.connect(tmp_path / "connector.db").execute("SELECT password FROM users").fetchone()[0]
    assert stored.startswith("scrypt$") and "wachtwoord" not in stored
    bob = users.redeem(users.invite(), "bob", 10, "bobs-wachtwoord")
    assert users.by_password("bob", "bobs-wachtwoord") == {k: users.by_token(bob)[k] for k in ("id", "name", "invite")}


def test_a_database_of_version_2_gets_the_password_column(tmp_path):
    db = sqlite3.connect(tmp_path / "connector.db")
    db.executescript("CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, token TEXT NOT NULL UNIQUE, "
                     "created INTEGER NOT NULL, invite TEXT); INSERT INTO users VALUES ('u1', 'anna', 'x', 1, NULL); "
                     "PRAGMA user_version = 2;")
    db.close()
    users = Users(tmp_path)
    assert users.set_password("anna", "lang-genoeg-wachtwoord") and users.by_password("anna", "lang-genoeg-wachtwoord")
    assert sqlite3.connect(tmp_path / "connector.db").execute("PRAGMA user_version").fetchone()[0] == 3


def test_the_admin_sets_a_password_without_seeing_it(tmp_path, monkeypatch, capsys):
    Users(tmp_path).add("anna")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["users", "password", "anna"])
    answers = iter(["lang-genoeg-wachtwoord", "lang-genoeg-wachtwoord"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    main()
    assert "Password set for anna." in capsys.readouterr().out
    assert Users(tmp_path).by_password("anna", "lang-genoeg-wachtwoord")
    answers = iter(["lang-genoeg-wachtwoord", "anders-dan-eerst"])
    with pytest.raises(SystemExit, match="differ"):
        main()


def test_a_sign_up_without_a_working_invite_costs_no_slow_hash(tmp_path, monkeypatch):
    # Anyone can post to /signup/<anything>: the 16 MiB scrypt comes only after the invite checks out.
    import connector.users as module

    hashed = []
    monkeypatch.setattr(module, "hash_password", lambda password: hashed.append(password) or "x")
    with pytest.raises(module.SignupError, match="no longer works"):
        Users(tmp_path).redeem("verzonnen", "carla", 10, "lang-genoeg-wachtwoord")
    assert hashed == []
