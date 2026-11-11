"""Logging in from Claude with OAuth 2.1, as MCP asks. Nobody has to copy a token.

1. Claude registers itself (`/register`) and sends the user to `/authorize`.
2. We redirect to `/login/<request>`. There the user logs in with one click: with the extension (which already
   has the API token), with the cookie from an earlier time, or by pasting the token.
3. Claude gets a code, and exchanges it (with PKCE) at `/token` for an access and refresh token.

Clients and tokens are in connector.db (tokens as SHA-256): a restart or deploy logs nobody out. An open
request and a code live only in memory: ten and five minutes. The API token (`usc_…`) keeps working.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from mcp.server.auth.handlers import revoke as _revoke
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .users import Users

ACCESS_TTL = 86_400
REFRESH_TTL = 90 * 86_400
CODE_TTL = 300
REQUEST_TTL = 600
MAX_REQUESTS = 1000  # open login requests; more is an attack, not a busy day
MAX_CLIENT = 8_000  # characters per registration; a real one is a few hundred
COOKIE = "usc_login"
COOKIE_TTL = 30 * 86_400
SCOPES = ["mcp"]


class _Revocation(_revoke.RevocationRequest):
    client_secret: str | None = None


# ponytail: the SDK (2.3.0) demands a client_secret on /revoke, also from a client without one (like Claude Code); RFC 7009
# does not. Remove once the SDK makes that field optional; test_logging_out_from_claude_ends_the_login notices.
_revoke.RevocationRequest = _Revocation


@dataclass
class Pending:
    """A login request that waits for the user."""
    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires: float

    @property
    def client_name(self) -> str:
        return (self.client.client_name or "An app")[:60]  # the client picks the name: no wall of text on the page

    @property
    def back_to(self) -> str:
        """Where the user goes after logging in. The page shows it, so a fake request stands out."""
        uri = urlsplit(str(self.params.redirect_uri))
        return uri.hostname or uri.scheme

    @property
    def source(self) -> str:
        """The redirect as a CSP source. Chrome blocks a redirect after a form if form-action does not allow it."""
        uri = urlsplit(str(self.params.redirect_uri))
        if uri.scheme in ("http", "https"):
            netloc = uri.netloc.rpartition("@")[2]
            return f"{uri.scheme}://{netloc}" if re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", netloc) else ""
        return f"{uri.scheme}:" if re.fullmatch(r"[a-z][a-z0-9+.\-]*", uri.scheme) else ""


class Client(OAuthClientInformationFull):
    """Scopes mean nothing here: a token gives access to everything of that user. So a client gets every
    scope it asks for, also one other than at its registration. That way no client trips over a scope name."""

    def validate_scope(self, requested_scope: str | None) -> list[str] | None:
        return requested_scope.split() if requested_scope else None


class OAuth:
    """The `OAuthAuthorizationServerProvider` of the MCP SDK. The SDK checks PKCE, redirect and expiry."""

    def __init__(self, users: Users, public_url: str, key: bytes):
        self.users, self.login_url = users, f"{public_url}/login"
        self.key = hmac.new(key, b"login", hashlib.sha256).digest()  # not the vault key itself
        self.requests: dict[str, Pending] = {}
        self.done: dict[str, tuple[float, str]] = {}  # used requests, for ten minutes: when, and the app's name
        self.codes: dict[str, AuthorizationCode] = {}
        self.lock = threading.Lock()

    # ---------- clients ----------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = self.users.client(client_id)
        return Client.model_validate_json(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if len(info := client_info.model_dump_json()) > MAX_CLIENT:  # the SDK allows 4 MB, and we keep it
            raise RegistrationError("invalid_client_metadata", "This registration is too large.")
        self.users.add_client(client_info.client_id, info)

    # ---------- logging in ----------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request_id = secrets.token_urlsafe(24)
        with self.lock:
            self._prune()
            if len(self.requests) >= MAX_REQUESTS:  # the oldest makes way: a flood of requests locks nobody out
                del self.requests[next(iter(self.requests))]
            self.requests[request_id] = Pending(client, params, time.time() + REQUEST_TTL)
        return f"{self.login_url}/{request_id}"

    def pending(self, request_id: str) -> Pending | None:
        with self.lock:
            request = self.requests.get(request_id)
        return request if request and request.expires > time.time() else None

    def approve(self, request_id: str, user_id: str) -> str | None:
        """The user logged in: a code for the client. A request works once."""
        with self.lock:
            request = self.requests.pop(request_id, None)
            if not request or request.expires < time.time():
                return None
            self.done[request_id] = (time.time() + REQUEST_TTL, request.client_name)
            code = secrets.token_urlsafe(32)
            params = request.params
            self.codes[code] = AuthorizationCode(
                code=code, scopes=params.scopes or (request.client.scope or "").split(),
                expires_at=time.time() + CODE_TTL, client_id=request.client.client_id,
                code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=params.resource, subject=user_id)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def used(self, request_id: str) -> str | None:
        """The app this request logged in to, in the last ten minutes. The login page may load again after that:
        the browser goes back, or the app sends it back."""
        with self.lock:
            done = self.done.get(request_id)
        return done[1] if done and done[0] > time.time() else None

    def deny(self, request_id: str) -> str | None:
        with self.lock:
            request = self.requests.pop(request_id, None)
        if not request:
            return None
        return construct_redirect_uri(str(request.params.redirect_uri), error="access_denied", state=request.params.state)

    def _prune(self) -> None:
        now = time.time()
        self.requests = {k: r for k, r in self.requests.items() if r.expires > now}
        self.done = {k: d for k, d in self.done.items() if d[0] > now}
        self.codes = {k: c for k, c in self.codes.items() if c.expires_at > now}

    # ---------- tokens ----------

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str
                                      ) -> AuthorizationCode | None:
        with self.lock:
            code = self.codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
                                          ) -> OAuthToken:
        with self.lock:
            if self.codes.pop(authorization_code.code, None) is None:
                raise TokenError("invalid_grant", "This code was already used.")
        pair = self.users.grant(client.client_id, authorization_code.subject or "", authorization_code.scopes,
                                authorization_code.resource, ACCESS_TTL, REFRESH_TTL)
        return _token(pair, authorization_code.scopes)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        grant = self.users.granted(refresh_token, "refresh")
        if not grant or grant["client"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=grant["client"], scopes=grant["scopes"],
                            expires_at=grant["expires"], resource=grant["resource"], subject=grant["user"])

    # ponytail: a second use of the same refresh token only fails; revoke the whole login if theft becomes a real issue.
    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        if not (pair := self.users.rotate(refresh_token.token, scopes, ACCESS_TTL, REFRESH_TTL)):
            raise TokenError("invalid_grant", "This refresh token no longer works. Log in again.")
        return _token(pair, scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        grant = self.users.granted(token, "access")
        return access(token, grant["client"], grant["user"], grant["name"], grant["scopes"], grant["expires"]) if grant else None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.users.revoke(token.token)

    # ---------- the cookie on /login ----------
    # Who logged in before (or just made an account) does not have to do it again in this browser. The cookie is
    # signed with the API token and the password in it: a new token (`users add`) or password makes it invalid.

    def cookie(self, user_id: str) -> str:
        expires = str(int(time.time()) + COOKIE_TTL)
        return f"{user_id}.{expires}.{self._sign(user_id, expires, self.users.login_secret(user_id))}"

    def from_cookie(self, value: str | None) -> dict | None:
        parts = (value or "").split(".")
        if len(parts) != 3 or not (parts[1].isascii() and parts[1].isdigit()) or int(parts[1]) < time.time():
            return None
        user_id, expires, signature = parts
        if not (secret := self.users.login_secret(user_id)):
            return None
        return self.users.by_id(user_id) if hmac.compare_digest(signature, self._sign(user_id, expires, secret)) else None

    def _sign(self, user_id: str, expires: str, secret: str) -> str:
        return hmac.new(self.key, f"{user_id}.{expires}.{secret}".encode(), hashlib.sha256).hexdigest()


def access(token: str, client_id: str, user_id: str, name: str, scopes: list[str], expires: int | None = None
           ) -> AccessToken:
    """Who uses a token: `subject` is the user's id, the name is in `claims`."""
    return AccessToken(token=token, client_id=client_id, subject=user_id, scopes=scopes, expires_at=expires,
                       claims={"name": name})


def _token(pair: tuple[str, str], scopes: list[str]) -> OAuthToken:
    return OAuthToken(access_token=pair[0], token_type="Bearer", expires_in=ACCESS_TTL,  # noqa: S106
                      refresh_token=pair[1], scope=" ".join(scopes) or None)
