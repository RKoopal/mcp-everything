"""The browser runtime: a tool runs as a `fetch` in a real Chrome on the server.

For sites that refuse a request from outside the browser. Every call gets its own, empty
browser context with only this user's cookies for this site; after the call it is gone.
The browser does not pretend to be anything else: no stealth, no faked headers (rule 3).
If a site refuses a headless browser in a data center, that is the answer.

Chrome runs on its own (docker: chromedp/headless-shell) and only the connector can reach it.
`Browser` has the same `request` as `httpx2.Client`, so the `Runner` does not know where it runs.
"""
from __future__ import annotations

import itertools
import json
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from urllib.parse import urlencode, urlsplit

import httpx2
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from .auth import cookie_pairs
from .errors import ConnectorError
from .runner import TIMEOUT, guard, public

LOAD_TIMEOUT = 20
FETCH = """async ({url, method, headers, body}) => {
  const r = await fetch(url, {method, headers, body, credentials: "include"});
  return {status: r.status, text: await r.text()};
}"""


@dataclass
class Response:
    status_code: int
    text: str
    cookies: dict

    @property
    def content(self) -> bytes:
        return self.text.encode()

    def json(self):
        return json.loads(self.text)


def reachable(url: str, allow_private: bool = False) -> bool:
    """May the browser go here? A page also loads from other domains (a CDN), but never from an internal address."""
    target = urlsplit(url)
    if target.scheme not in ("http", "https"):
        return False
    return allow_private or (target.scheme == "https" and public(target.hostname or ""))


class CDP:
    """Commands to Chrome over one websocket. Events in between go to `on_event`, or are skipped."""

    def __init__(self, ws, timeout: float, on_event: Callable[[CDP, dict], None] | None = None):
        self.ws, self.timeout, self.ids, self.on_event = ws, timeout, itertools.count(1), on_event

    def send(self, method: str, params: dict | None = None, session: str | None = None) -> int:
        """Without waiting for the answer."""
        number = next(self.ids)
        message = {"id": number, "method": method, "params": params or {}}
        self.ws.send(json.dumps(message | ({"sessionId": session} if session else {})))
        return number

    def __call__(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        number = self.send(method, params, session)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                reply = json.loads(self.ws.recv(timeout=max(deadline - time.monotonic(), 0.01)))
            except TimeoutError:
                raise httpx2.ReadTimeout(f"browser: {method}") from None
            if reply.get("id") == number:
                if "error" in reply:
                    raise ConnectorError(f"Browser: {reply['error'].get('message', 'unknown error')}")
                return reply.get("result", {})
            if "method" in reply and self.on_event:
                self.on_event(self, reply)


class Browser:
    """`url` is Chrome's DevTools address, such as http://browser:9222."""

    def __init__(self, url: str, base: str, allow_private: bool = False):
        self.url, self.check = url, guard(base, allow_private)
        self.origin = base.rstrip("/")
        self.reachable = cache(lambda url: reachable(url, allow_private))

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        """Nothing to close: every call has its own connection."""

    def _endpoint(self) -> str:
        # Chrome refuses DevTools for a Host header that is not an IP or localhost, so go through the IP.
        url = urlsplit(self.url)
        address = f"{socket.gethostbyname(url.hostname)}:{url.port or 9222}"
        info = httpx2.get(f"http://{address}/json/version", timeout=5).json()
        return urlsplit(info["webSocketDebuggerUrl"])._replace(netloc=address).geturl()

    def request(self, method: str, url: str, params: dict | None = None, headers: dict | None = None,
                json: object = None, data: dict | None = None, timeout: float = TIMEOUT) -> Response:
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        self.check(url)
        # A fetch cannot set cookies itself; they go into the context of this call.
        cookies = next((cookie_pairs(v) for k, v in (headers or {}).items() if k.lower() == "cookie"), {})
        headers = {k: v for k, v in (headers or {}).items() if k.lower() != "cookie"}
        body = None
        if json is not None:
            body = _json_dumps(json)
            headers.setdefault("Content-Type", "application/json")
        elif data is not None:
            body = urlencode(data)
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        try:
            with connect(self._endpoint(), max_size=None, open_timeout=10) as ws:
                return self._fetch(CDP(ws, timeout, self._paused), {"url": url, "method": method, "headers": headers,
                                                                    "body": body}, cookies)
        except (OSError, WebSocketException) as e:
            raise httpx2.ConnectError(f"browser: {type(e).__name__}") from None

    def _fetch(self, cdp: CDP, call: dict, cookies: dict) -> Response:
        context = cdp("Target.createBrowserContext", {"disposeOnDetach": True})["browserContextId"]
        try:
            if cookies:
                cdp("Storage.setCookies", {"browserContextId": context, "cookies": [
                    # HttpOnly: scripts of the page (analytics, a tag manager) never read the session.
                    {"name": k, "value": v, "url": self.origin + "/", "secure": self.origin.startswith("https"),
                     "httpOnly": True}
                    for k, v in cookies.items()]})
            target = cdp("Target.createTarget", {"url": "about:blank", "browserContextId": context})["targetId"]
            session = cdp("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
            # Every request of the page passes `_paused` first, also after a redirect: guard only sees the first one. Frames
            # in their own process and workers are targets of their own: they wait until `_paused` checks them too.
            self._watch(cdp, session)
            # The fetch must come from the site itself, or it is a request from a foreign page.
            cdp("Page.navigate", {"url": self.origin + "/"}, session)
            self._wait_for_page(cdp, session)
            # In a world of its own: a script of the page that wraps window.fetch never sees the headers or the answer.
            frame = cdp("Page.getFrameTree", {}, session)["frameTree"]["frame"]["id"]
            world = cdp("Page.createIsolatedWorld", {"frameId": frame, "worldName": "mcp-everything"},
                        session)["executionContextId"]
            result = cdp("Runtime.evaluate", {"expression": f"({FETCH})({_json_dumps(call)})", "contextId": world,
                                              "awaitPromise": True, "returnByValue": True}, session)
            if "exceptionDetails" in result:
                raise httpx2.ConnectError("browser: fetch failed")
            answer = result["result"]["value"]
            host = urlsplit(self.origin).hostname
            jar = cdp("Storage.getCookies", {"browserContextId": context})["cookies"]
            mine = {c["name"]: c["value"] for c in jar if host == c["domain"].lstrip(".") or
                    host.endswith("." + c["domain"].lstrip("."))}
            return Response(answer["status"], answer["text"], mine)
        finally:
            cdp("Target.disposeBrowserContext", {"browserContextId": context})

    @staticmethod
    def _watch(cdp: CDP, session: str) -> None:
        cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}, session)
        cdp.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}, session)

    def _paused(self, cdp: CDP, event: dict) -> None:
        """A request of the page waits for us: go on if the address is allowed, refuse it otherwise (SSRF)."""
        params, session = event["params"], event.get("sessionId")
        if event["method"] == "Target.attachedToTarget":  # a frame or worker: check its requests too, then let it run
            child = params["sessionId"]
            self._watch(cdp, child)
            cdp.send("Runtime.runIfWaitingForDebugger", {}, child)
            return
        if event["method"] != "Fetch.requestPaused":
            return
        if self.reachable(params["request"]["url"]):
            cdp.send("Fetch.continueRequest", {"requestId": params["requestId"]}, session)
        else:
            cdp.send("Fetch.failRequest", {"requestId": params["requestId"], "errorReason": "BlockedByClient"}, session)

    def _wait_for_page(self, cdp: CDP, session: str) -> None:
        deadline = time.monotonic() + LOAD_TIMEOUT
        while time.monotonic() < deadline:
            state = cdp("Runtime.evaluate", {"expression": "[location.origin, document.readyState]",
                                             "returnByValue": True}, session)["result"].get("value")
            if state and state[0] == self.origin and state[1] != "loading":
                return
            if state and state[0] not in ("null", self.origin) and state[1] == "complete":
                raise ConnectorError("The site sends the browser to another domain.")
            time.sleep(0.2)
        raise httpx2.ReadTimeout("browser: page does not load")


def _json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
