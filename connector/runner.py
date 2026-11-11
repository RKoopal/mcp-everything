"""Runs tools with the rules that hold for every connector.

Reads may be retried, writes never: a retried write can run twice. After a
401 or 403 the session is renewed once. Error messages never name the URL: it can hold a
session. A request only goes to the site from the manifest (see `guard`).
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx2

from . import auth
from .errors import AuthExpired, ConnectorError, StateUnknown, Unverified
from .ledger import Ledger
from .manifest import extract, render, verified
from .store import Store

TIMEOUT = 30
TRANSIENT = {429, 500, 502, 503, 504}
REPEATABLE = {"read", "preview"}
OPEN_STATES = {"started", "unknown", "unverified"}  # outcome unknown: check it yourself first
_CLAIMS: defaultdict = defaultdict(threading.Lock)


def public(host: str) -> bool:
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return False
    return all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)


def guard(base: str, allow_private: bool = False) -> Callable[[str], None]:
    """Refuses requests outside the manifest's site, and on a server also internal addresses (SSRF)."""
    site = urlsplit(base)

    def check(url: str) -> None:
        target = urlsplit(url)
        if (target.scheme, target.hostname, target.port) != (site.scheme, site.hostname, site.port):
            raise ConnectorError("Refused a request outside the site.")
        # ponytail: the address is looked up here and then again by httpx2. A DNS rebind in between
        # slips through; pin the IP in a custom transport if the registry ever gets unknown contributors.
        if not allow_private and (target.scheme != "https" or not public(target.hostname)):
            raise ConnectorError("Only https to public addresses.")
    return check


def http_client(base: str, allow_private: bool = False) -> httpx2.Client:
    """One client per call: the httpx2 cookie jar must never mix sessions of users."""
    check = guard(base, allow_private)
    return httpx2.Client(follow_redirects=True, event_hooks={"request": [lambda r: check(str(r.url))]})


def _parse(r):
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:
        return r.text


class Runner:
    def __init__(self, manifest: dict, store: Store, http, sleep: Callable[[float], None] = time.sleep):
        self.manifest, self.tools = manifest, {t["name"]: t for t in manifest["tools"]}
        self.http = http
        self.auth = auth.for_manifest(manifest, store, self.send)
        self.ledger = Ledger(store)
        self.attempts, self.sleep = manifest.get("attempts", 3), sleep

    def send(self, request: dict, values: dict):
        """One request without session and without retries: for logins and tokens."""
        return self.http.request(**render(self.manifest, request, values), timeout=TIMEOUT)

    def _request(self, tool: dict, args: dict):
        properties = tool["input_schema"].get("properties", {})
        args = {k: p["default"] for k, p in properties.items() if "default" in p} | args
        req = render(self.manifest, tool["request"], args)
        if tool.get("session", True):  # searching without an account: the site then does not tie it to you
            req["headers"] |= self.auth.headers()
        return self.http.request(**req, timeout=TIMEOUT)

    def _send(self, tool: dict, args: dict):
        name, effect = tool["name"], tool["effect"]
        if tool.get("session", True) and not self.auth.connected():
            raise AuthExpired(f"{name}: not linked")
        attempts = self.attempts if effect in REPEATABLE else 1
        attempt, renewed = 0, False
        while True:
            attempt += 1
            try:
                r = self._request(tool, args)
            except httpx2.TransportError as e:
                if effect == "write":
                    raise StateUnknown(f"{name}: no answer ({type(e).__name__})") from None
                if attempt >= attempts:
                    raise ConnectorError(f"{name}: no answer ({type(e).__name__})") from None
                self.sleep(2 ** attempt)
                continue
            if tool.get("session", True):  # cookies from an anonymous request do not belong to your session
                self.auth.seen(r)
            if r.status_code in (401, 403):
                # A refused request was not done, so this is fine for writes too.
                if not renewed and self.auth.renew():
                    renewed = True
                    continue
                raise AuthExpired(f"{name}: session expired or refused ({r.status_code})")
            if effect == "write" and r.status_code >= 500:
                raise StateUnknown(f"{name}: HTTP {r.status_code}, may have been done anyway")
            if r.status_code in TRANSIENT and attempt < attempts:
                self.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise ConnectorError(f"{name}: HTTP {r.status_code}: {r.text[:200]}")
            return _parse(r)

    def call(self, name: str, args: dict | None = None):
        """Reads and previews."""
        tool = self.tools[name]
        if tool["effect"] == "write":
            raise ConnectorError(f"{name} is a write: use write().")
        return extract(tool.get("response"), self._send(tool, args or {}))

    def write(self, name: str, args: dict, key: str, confirmed: bool = False):
        """Runs once and checks. `key` belongs to one intention, not to the item.
        A human_only action only goes ahead when the user confirmed it themselves (`confirmed`)."""
        tool = self.tools[name]
        if tool["effect"] != "write":
            raise ConnectorError(f"{name} is not a write.")
        if tool["human_only"] and not confirmed:
            raise ConnectorError(f"{name} needs the user to confirm. Nothing was sent.")
        with _CLAIMS[self.ledger.path]:  # two equal calls at once: one runs
            prior = self.ledger.get(key)
            if prior and prior["state"] == "done":
                return prior["result"]
            if prior and prior["state"] in OPEN_STATES:
                raise StateUnknown(f"{key}: the outcome of the last try is unknown. Check it on the site first.")
            self.ledger.add(key, "started", tool=name)
        try:
            data = self._send(tool, args)
        except StateUnknown:
            self.ledger.add(key, "unknown", tool=name)
            raise
        except ConnectorError as e:
            self.ledger.add(key, "rejected", tool=name, reason=str(e))
            raise
        if not verified(tool, data):
            self.ledger.add(key, "unverified", tool=name)
            raise Unverified(f"{key}: the site reported success, but the verifier does not see the result.")
        try:
            result = extract(tool.get("response"), data)
        except ConnectorError:
            result = data  # done and verified; only the shape of the response differs
        self.ledger.add(key, "done", tool=name, result=result)
        return result
