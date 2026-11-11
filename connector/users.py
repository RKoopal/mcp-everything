"""Accounts, invites and recordings, in SQLite: `<data>/connector.db`.

- An account has one API token. Only its SHA-256 is on disk. A token is 256 bits of
  randomness, so a slow hash adds nothing. Running `add` again with the same name gives a new token.
- And a password, for people: nobody remembers a token. Only its scrypt hash is on disk. Who signs up
  picks one; for an account from `add`, or a forgotten password, the admin sets one with `password`.
- An admin makes an account (`add`), or an invite (`invite`): a link with which a few
  people make an account themselves. Who comes through an invite gets limits; see `connector.server`.
- Every recording is in `recordings`: who, which site, which tools. That way you know who added what.
- Logging in from Claude (OAuth, see `connector.oauth`): the clients that registered, and their access and
  refresh tokens in `grants`, also as SHA-256.

    python -m connector.users add <name>
    python -m connector.users password <name>
    python -m connector.users invite [--uses 3] [--note demo]
    python -m connector.users list

`PRAGMA user_version` is the schema version, for a later migration.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

PREFIX = "usc_"
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{1,31}")
VERSION = 3  # 2: clients and grants for OAuth. 3: a password per user
SCHEMA = """
CREATE TABLE IF NOT EXISTS invites (
    id      TEXT PRIMARY KEY,
    code    TEXT NOT NULL UNIQUE,      -- SHA-256 of the code in the link
    uses    INTEGER NOT NULL,
    used    INTEGER NOT NULL DEFAULT 0,
    note    TEXT,
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    token   TEXT NOT NULL UNIQUE,      -- SHA-256 of the token
    created INTEGER NOT NULL,
    invite  TEXT REFERENCES invites(id), -- empty: made by an admin
    password TEXT                      -- scrypt$<salt>$<hash>, or empty: log in with the token
);
CREATE TABLE IF NOT EXISTS recordings (
    id       TEXT PRIMARY KEY,         -- the id the extension asks for
    user     TEXT NOT NULL REFERENCES users(id),
    host     TEXT NOT NULL,
    site     TEXT,
    new_site INTEGER NOT NULL DEFAULT 0,
    added    TEXT NOT NULL DEFAULT '[]',
    asked    INTEGER NOT NULL DEFAULT 0, -- 1 if Claude was called: only that costs money and counts
    error    TEXT,
    at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS recordings_by_user ON recordings (user, at);
CREATE TABLE IF NOT EXISTS clients (
    id      TEXT PRIMARY KEY,
    info    TEXT NOT NULL,             -- the registration as the client sent it, with our answer (JSON)
    created INTEGER NOT NULL,
    used    INTEGER NOT NULL DEFAULT 0 -- 1 once someone logged in with it: that client stays
);
CREATE TABLE IF NOT EXISTS grants (
    token    TEXT PRIMARY KEY,         -- SHA-256 of the access or refresh token
    kind     TEXT NOT NULL,            -- access | refresh
    login    TEXT NOT NULL,            -- one login; revoking removes everything of that login
    client   TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    user     TEXT NOT NULL REFERENCES users(id),
    scopes   TEXT NOT NULL,            -- JSON
    resource TEXT,
    expires  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS grants_by_login ON grants (login);
"""
ACCESS, REFRESH = "usa_", "usr_"


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _scrypt(password: str, salt: bytes) -> bytes:
    # A password is guessable, unlike a token: a slow hash, about 50 ms and 16 MB per try.
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    if not 10 <= len(password) <= 200:
        raise SignupError("Pick a password of at least 10 characters.")
    salt = secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${_scrypt(password, salt).hex()}"


def check_password(password: str, stored: str | None) -> bool:
    """Also without a stored password it hashes once: then a wrong name takes as long as a wrong password."""
    _, salt, digest = (stored or f"scrypt${'00' * 16}${'00' * 32}").split("$")
    return hmac.compare_digest(_scrypt(password[:200], bytes.fromhex(salt)).hex(), digest) and stored is not None


class SignupError(ValueError):
    pass


class Users:
    def __init__(self, data: Path):
        self.file = Path(data) / "connector.db"
        self.file.parent.mkdir(parents=True, exist_ok=True)
        if not self.file.exists():
            os.close(os.open(self.file, os.O_WRONLY | os.O_CREAT, 0o600))
        with closing(sqlite3.connect(self.file, timeout=10)) as db:
            db.executescript(f"BEGIN; {SCHEMA} PRAGMA user_version = {VERSION}; COMMIT;")
            if "password" not in {row[1] for row in db.execute("PRAGMA table_info(users)")}:  # from version 2
                db.execute("ALTER TABLE users ADD COLUMN password TEXT")
                db.commit()
        self._import(Path(data) / "users.json")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One transaction that takes the write lock at once: two sign-ups at the same time never get the same spot."""
        with closing(sqlite3.connect(self.file, timeout=10, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    def _import(self, old: Path) -> None:
        """Until 2026-11-05 the accounts were in users.json. They move over once, with their id and token."""
        if not old.exists():
            return
        with self._tx() as db:
            for user in json.loads(old.read_text()):
                db.execute("INSERT OR IGNORE INTO users (id, name, token, created) VALUES (?, ?, ?, ?)",
                           (user["id"], user["name"], user["token"], user["created"]))
        old.rename(old.with_suffix(".json.imported"))

    # ---------- accounts ----------

    def all(self) -> list[dict]:
        with self._tx() as db:
            return [dict(row) for row in db.execute("SELECT id, name, created, invite FROM users ORDER BY created")]

    def add(self, name: str) -> str:
        name = name.strip().lower()
        if not NAME.fullmatch(name):
            raise ValueError("Name: 2-32 characters, lowercase letters, digits, . _ -")
        token = PREFIX + secrets.token_urlsafe(32)
        with self._tx() as db:
            db.execute("INSERT INTO users (id, name, token, created) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT (name) DO UPDATE SET token = excluded.token",
                       (secrets.token_hex(8), name, _digest(token), int(time.time())))
            # A new token also logs out everywhere: whoever stole the old one keeps no OAuth login.
            db.execute("DELETE FROM grants WHERE user = (SELECT id FROM users WHERE name = ?)", (name,))
        return token

    def by_token(self, token: str) -> dict | None:
        if not token.startswith(PREFIX):
            return None
        with self._tx() as db:
            row = db.execute("SELECT id, name, invite FROM users WHERE token = ?", (_digest(token),)).fetchone()
        return dict(row) if row else None

    def by_password(self, name: str, password: str) -> dict | None:
        with self._tx() as db:
            row = db.execute("SELECT id, name, invite, password FROM users WHERE name = ?",
                             (name.strip().lower(),)).fetchone()
        if not check_password(password, row["password"] if row else None):
            return None
        return {k: row[k] for k in ("id", "name", "invite")}

    def set_password(self, name: str, password: str) -> bool:
        """For an account from `add`, or a forgotten or stolen password. False: no such account.

        It also logs out everywhere: every OAuth login ends, and the login cookie no longer works (`login_secret`)."""
        hashed, name = hash_password(password), name.strip().lower()
        with self._tx() as db:
            if db.execute("UPDATE users SET password = ? WHERE name = ?", (hashed, name)).rowcount != 1:
                return False
            db.execute("DELETE FROM grants WHERE user = (SELECT id FROM users WHERE name = ?)", (name,))
        return True

    def login_secret(self, user_id: str) -> str | None:
        """What the login cookie is signed with: the hashes of the API token and the password. A new one of either
        makes the cookie invalid."""
        with self._tx() as db:
            row = db.execute("SELECT token, password FROM users WHERE id = ?", (user_id,)).fetchone()
        return f"{row['token']}.{row['password'] or ''}" if row else None

    def by_id(self, user_id: str) -> dict | None:
        """With `token`: the hash of the API token. That way a new token also makes a login cookie invalid."""
        with self._tx() as db:
            row = db.execute("SELECT id, name, token FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def invited(self, user_id: str) -> bool:
        """Did this user come through an invite? Then the limits apply."""
        with self._tx() as db:
            row = db.execute("SELECT invite FROM users WHERE id = ?", (user_id,)).fetchone()
        return bool(row and row["invite"])

    # ---------- invites ----------

    def invite(self, uses: int = 1, note: str | None = None) -> str:
        """A code for the link /signup/<code>. Works `uses` times. Only the hash is stored."""
        if uses < 1:
            raise ValueError("An invite works at least once.")
        code = secrets.token_urlsafe(16)
        with self._tx() as db:
            db.execute("INSERT INTO invites (id, code, uses, note, created) VALUES (?, ?, ?, ?, ?)",
                       (secrets.token_hex(8), _digest(code), uses, note, int(time.time())))
        return code

    def open_invite(self, code: str, max_users: int) -> bool:
        with self._tx() as db:
            return self._open(db, code, max_users) is not None

    def _open(self, db: sqlite3.Connection, code: str, max_users: int) -> str | None:
        row = db.execute("SELECT id FROM invites WHERE code = ? AND used < uses", (_digest(code),)).fetchone()
        full = db.execute("SELECT count(*) FROM users").fetchone()[0] >= max_users
        return row["id"] if row and not full else None

    def redeem(self, code: str, name: str, max_users: int, password: str | None = None) -> str:
        """Makes an account with an invite. Returns the token, or a SignupError that says what is wrong.
        The pages always pass a password; without one, the account logs in with its token only."""
        name = name.strip().lower()
        if not NAME.fullmatch(name):
            raise SignupError("Pick a name of 2 to 32 characters: lowercase letters, digits, dot, dash or _.")
        if not self.open_invite(code, max_users):  # before the slow hash: without an invite it costs nothing
            raise SignupError("This invite no longer works.")
        hashed = hash_password(password) if password is not None else None
        token = PREFIX + secrets.token_urlsafe(32)
        with self._tx() as db:
            if not (invite := self._open(db, code, max_users)):
                raise SignupError("This invite no longer works.")
            if db.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
                raise SignupError("That name is taken. Pick another one.")
            db.execute("INSERT INTO users (id, name, token, created, invite, password) VALUES (?, ?, ?, ?, ?, ?)",
                       (secrets.token_hex(8), name, _digest(token), int(time.time()), invite, hashed))
            db.execute("UPDATE invites SET used = used + 1 WHERE id = ?", (invite,))
        return token

    def invites(self) -> list[dict]:
        with self._tx() as db:
            return [dict(row) for row in db.execute("SELECT id, uses, used, note, created FROM invites ORDER BY created")]

    # ---------- recordings ----------

    def record(self, job: str, user_id: str, host: str) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO recordings (id, user, host, at) VALUES (?, ?, ?, ?)",
                       (job, user_id, host, int(time.time())))

    def recorded(self, job: str, site: str | None = None, new_site: bool = False, added: Sequence[str] = (),
                 error: str | None = None) -> None:
        with self._tx() as db:
            db.execute("UPDATE recordings SET site = ?, new_site = ?, added = ?, error = ? WHERE id = ?",
                       (site, int(new_site), json.dumps(list(added)), error, job))

    def asked(self, job: str) -> None:
        with self._tx() as db:
            db.execute("UPDATE recordings SET asked = 1 WHERE id = ?", (job,))

    def recordings_since(self, since: float, user_id: str) -> int:
        """Recordings for which Claude was called. A recording without API requests costs nothing and does not count."""
        with self._tx() as db:
            return db.execute("SELECT count(*) FROM recordings WHERE user = ? AND asked = 1 AND at >= ?",
                              (user_id, int(since))).fetchone()[0]

    # ---------- OAuth: clients and tokens ----------

    def add_client(self, client_id: str, info: str, keep: int = 1000) -> None:
        """Anyone may register (that is how MCP works). Of the clients nobody logged in with yet, only the
        newest `keep` stay: that way a flood of registrations does not fill the disk."""
        with self._tx() as db:
            db.execute("DELETE FROM clients WHERE used = 0 AND rowid NOT IN "
                       "(SELECT rowid FROM clients WHERE used = 0 ORDER BY rowid DESC LIMIT ?)", (keep - 1,))
            db.execute("INSERT INTO clients (id, info, created) VALUES (?, ?, ?)", (client_id, info, int(time.time())))

    def client(self, client_id: str) -> str | None:
        with self._tx() as db:
            row = db.execute("SELECT info FROM clients WHERE id = ?", (client_id,)).fetchone()
        return row["info"] if row else None

    def grant(self, client_id: str, user_id: str, scopes: Sequence[str], resource: str | None,
              access_ttl: int, refresh_ttl: int) -> tuple[str, str]:
        """A new access and refresh token for one login. Expired tokens go away at once."""
        with self._tx() as db:
            db.execute("DELETE FROM grants WHERE expires < ?", (int(time.time()),))
            db.execute("UPDATE clients SET used = 1 WHERE id = ?", (client_id,))
            return self._pair(db, secrets.token_hex(8), client_id, user_id, scopes, resource, access_ttl, refresh_ttl)

    def _pair(self, db: sqlite3.Connection, login: str, client_id: str, user_id: str, scopes: Sequence[str],
              resource: str | None, access_ttl: int, refresh_ttl: int) -> tuple[str, str]:
        access, refresh, now = ACCESS + secrets.token_urlsafe(32), REFRESH + secrets.token_urlsafe(32), int(time.time())
        for token, kind, ttl in ((access, "access", access_ttl), (refresh, "refresh", refresh_ttl)):
            db.execute("INSERT INTO grants (token, kind, login, client, user, scopes, resource, expires) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (_digest(token), kind, login, client_id, user_id, json.dumps(list(scopes)), resource, now + ttl))
        return access, refresh

    def granted(self, token: str, kind: str) -> dict | None:
        """A valid token: of which client and user, with the name."""
        if not token.startswith(ACCESS if kind == "access" else REFRESH):
            return None
        with self._tx() as db:
            row = db.execute("SELECT g.login, g.client, g.user, u.name, g.scopes, g.resource, g.expires FROM grants g "
                             "JOIN users u ON u.id = g.user WHERE g.token = ? AND g.kind = ? AND g.expires >= ?",
                             (_digest(token), kind, int(time.time()))).fetchone()
        return {**dict(row), "scopes": json.loads(row["scopes"])} if row else None

    def rotate(self, refresh: str, scopes: Sequence[str], access_ttl: int, refresh_ttl: int) -> tuple[str, str] | None:
        """A refresh token works once: the old pair goes, a new one comes. Twice at the same time: one wins."""
        with self._tx() as db:
            row = db.execute("SELECT login, client, user, resource FROM grants WHERE token = ? AND kind = 'refresh' "
                             "AND expires >= ?", (_digest(refresh), int(time.time()))).fetchone()
            if not row:
                return None
            db.execute("DELETE FROM grants WHERE login = ?", (row["login"],))
            return self._pair(db, row["login"], row["client"], row["user"], scopes, row["resource"], access_ttl, refresh_ttl)

    def revoke(self, token: str) -> None:
        """Removes the access and refresh token of the same login."""
        with self._tx() as db:
            db.execute("DELETE FROM grants WHERE login = (SELECT login FROM grants WHERE token = ?)", (_digest(token),))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m connector.users")
    parser.add_argument("command", choices=["add", "password", "invite", "list"])
    parser.add_argument("name", nargs="?")
    parser.add_argument("--uses", type=int, default=1, help="how many times an invite works (default 1)")
    parser.add_argument("--note", help="who the invite is for")
    args = parser.parse_args()
    users = Users(Path(os.environ.get("DATA_DIR", "data")))
    if args.command == "list":
        for u in users.all():
            print(u["name"], "(invited)" if u["invite"] else "")
        for i in users.invites():
            note = f" ({i['note']})" if i["note"] else ""
            print(f"invite {i['id']}: {i['used']}/{i['uses']} used{note}")
        return
    if args.command == "invite":
        url = os.environ.get("PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
        times = "once" if args.uses == 1 else f"{args.uses} times"
        print(f"Invite, works {times} (shown only once):\n{url}/signup/{users.invite(args.uses, args.note)}")
        return
    if not args.name:
        parser.error(f"{args.command} needs a name")
    if args.command == "password":
        password = getpass.getpass(f"New password for {args.name}: ")
        if getpass.getpass("Again: ") != password:
            raise SystemExit("The two passwords differ. Nothing changed.")
        try:
            done = users.set_password(args.name, password)
        except SignupError as e:
            raise SystemExit(str(e)) from None
        if not done:
            raise SystemExit(f"No account named {args.name}.")
        print(f"Password set for {args.name}.")
        return
    print(f"Token for {args.name} (shown only once):\n{users.add(args.name)}")


if __name__ == "__main__":
    main()
