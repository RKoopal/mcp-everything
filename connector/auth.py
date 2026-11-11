"""How a tool logs in. The kind is in the manifest (`auth.type`), the values per user in the vault.

- none: no session.
- session: cookies and headers from the user's browser. Only a person can renew it.
- password: like session, but when the session expires the connector logs in again itself with the
  stored details (WieBetaaltWat).
- oauth: access token with a rotating refresh token (Albert Heijn).

A person does the login itself: in the browser, or on the server's connect page.
"""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable

from .store import Store

SESSION = "session.json"        # {"cookies": {...}, "headers": {...}}
CREDENTIALS = "credentials.json"
TOKENS = "tokens.json"
# We never send bot-protection cookies: that would get around the protection (rule 3).
BOT_COOKIES = re.compile(r"aws-waf-token|_abck|bm_\w+|ak_bmsc|datadome|cf_clearance|__cf_bm|_px\w*|"
                         r"incap_ses_\w+|visid_incap_\w+|reese84|_dd_s", re.I)
# The old refresh token works only once, so never refresh twice at the same time; and never log in twice at
# the same time with a password. One lock per vault.
_REFRESH_LOCKS: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

Send = Callable[[dict, dict], object]  # (request from the manifest, values) -> response


def cookie_pairs(header: str) -> dict:
    """`a=1; b=2` -> {"a": "1", "b": "2"}"""
    return dict(part.strip().split("=", 1) for part in header.split(";") if "=" in part)


def cookies_of(response) -> dict:
    """The cookies from Set-Cookie, without bot-protection ones. httpx2 gives a jar, the browser a dict."""
    jar = getattr(response.cookies, "jar", None)
    found = {c.name: c.value for c in jar} if jar is not None else dict(response.cookies)
    return {k: v for k, v in found.items() if v and not BOT_COOKIES.fullmatch(k)}


def tokens(answer: dict) -> dict:
    return {"access_token": answer["access_token"], "refresh_token": answer["refresh_token"],
            "expires_at": time.time() + int(answer.get("expires_in", 3600)) - 120}


class Auth:
    """No session. The other kinds override what they need."""

    def connect(self, value) -> bool:
        """Links an account with what a person just entered. Returns False if the site refuses it."""
        return True

    def connected(self) -> bool:
        """Is there something to log in with? If not, no request goes to the site."""
        return True

    def headers(self) -> dict:
        return {}

    def renew(self) -> bool:
        return False

    def seen(self, response) -> None:
        """Every response passes here, for sites that renew the session along the way."""


class SessionAuth(Auth):
    def __init__(self, store: Store):
        self.store = store

    def connect(self, session: dict) -> bool:
        cookies = {k: v for k, v in session.get("cookies", {}).items() if v and not BOT_COOKIES.fullmatch(k)}
        session = {"cookies": cookies, "headers": session.get("headers", {})}
        if not cookies and not session["headers"]:
            return False
        self.store.write(SESSION, session)
        return True

    def connected(self) -> bool:
        return self.store.path(SESSION).exists()

    def headers(self) -> dict:
        session = self.store.read(SESSION, {})
        headers = dict(session.get("headers", {}))
        if cookies := session.get("cookies"):
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        return headers

    def seen(self, response) -> None:
        if not (new := cookies_of(response)):
            return
        with _REFRESH_LOCKS[str(self.store.dir)]:  # read and write together: otherwise a cookie gets lost
            if not (session := self.store.read(SESSION)):
                return  # a session only starts with connect, by a person
            cookies = session.get("cookies", {})
            if new.items() - cookies.items():
                self.store.write(SESSION, {**session, "cookies": cookies | new})


class PasswordAuth(SessionAuth):
    def __init__(self, store: Store, login: Callable[[dict], object]):
        super().__init__(store)
        self.login = login

    def connect(self, credentials: dict) -> bool:
        """A login is never retried: a repeated login can lock an account."""
        response = self.login(credentials)
        cookies = cookies_of(response)
        if response.status_code >= 400 or not cookies:
            return False
        self.store.write(SESSION, {"cookies": cookies})
        self.store.write(CREDENTIALS, credentials)
        return True

    def connected(self) -> bool:
        return super().connected() or self.store.path(CREDENTIALS).exists()

    def renew(self) -> bool:
        """Logs in again, once. If the site refuses the details (a new password), the connector forgets
        them: otherwise every call is a failed login, and that can lock the account. A person then links
        again. On an outage (5xx), too many tries (429) or a timeout (408) they stay."""
        seen = self.store.read(SESSION)
        with _REFRESH_LOCKS[str(self.store.dir)]:
            if (latest := self.store.read(SESSION)) and latest != seen:
                return True  # another request just logged in again
            if not (credentials := self.store.read(CREDENTIALS)):
                return False
            response = self.login(credentials)
            if response.status_code < 400 and (cookies := cookies_of(response)):
                self.store.write(SESSION, {"cookies": cookies})
                return True
            if response.status_code < 500 and response.status_code not in (408, 429):
                self.store.delete(CREDENTIALS)
                self.store.delete(SESSION)
            return False


class OAuth(Auth):
    def __init__(self, store: Store, exchange: Callable[[str], dict | None], refresh: Callable[[str], dict | None]):
        self.store, self.exchange, self.refresh = store, exchange, refresh

    def connect(self, code: str) -> bool:
        """`code` comes from the site's login page, after a person logged in there."""
        if not (new := self.exchange(code)):
            return False
        self.store.write(TOKENS, new)
        return True

    def connected(self) -> bool:
        return self.store.path(TOKENS).exists()

    def headers(self) -> dict:
        tok = self.store.read(TOKENS)
        if not tok:
            return {}
        if time.time() >= tok["expires_at"] and self.renew():
            tok = self.store.read(TOKENS)
        return {"Authorization": f"Bearer {tok['access_token']}"}

    def renew(self) -> bool:
        seen = self.store.read(TOKENS)
        with _REFRESH_LOCKS[str(self.store.dir)]:
            latest = self.store.read(TOKENS)
            if not latest:
                return False
            if seen and latest["refresh_token"] != seen["refresh_token"]:
                return True  # another request just refreshed
            new = self.refresh(latest["refresh_token"])
            if not new:
                return False
            self.store.write(TOKENS, new)
            return True


def for_manifest(manifest: dict, store: Store, send: Send | None = None) -> Auth:
    """`send` runs a single request, without session and without retries. Only needed to log in."""
    auth = manifest.get("auth", {"type": "none"})

    def token(request: str, values: dict) -> dict | None:
        response = send(auth[request], values)
        try:
            return tokens(response.json()) if response.status_code == 200 else None
        except (ValueError, KeyError, TypeError):  # 200, but no tokens: the site did something else
            return None

    kinds = {"none": lambda: Auth(),
             "session": lambda: SessionAuth(store),
             "password": lambda: PasswordAuth(store, lambda credentials: send(auth["login"], credentials)),
             "oauth": lambda: OAuth(store, lambda code: token("token", {"code": code}),
                                    lambda refresh: token("refresh", {"refresh_token": refresh}))}
    return kinds[auth["type"]]()
