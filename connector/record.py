"""Standard recording: `python -m connector.record <name> <url> [--host bol.com]`.

Opens its own Chrome window with its own profile. Click what you want to test and close the
window. Chrome passes on every request at once, so nothing gets lost on a page change,
in a worker or while logging in.

First the recording makes one plain request without a browser: that is the access test. Raw content
stays in memory. Only recordings/<name>.json goes to disk, without values.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx2
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from .har import API_TYPES, summarize, template
from .store import Store

AGENT = "mcp-everything-test"
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BUFFERS = {"maxTotalBufferSize": 200_000_000, "maxResourceBufferSize": 20_000_000}
AUTO_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True,
               "filter": [{"type": "browser", "exclude": True}, {"type": "tab", "exclude": True}, {}]}


class Recorder:
    """Turns messages from Chrome into recording entries. `send` sends a command and returns its id."""

    def __init__(self, send, start_url: str):
        self.send, self.start_url = send, start_url
        self.entries: list[dict] = []
        self.open: dict[tuple, dict] = {}
        self.replies: dict[int, tuple] = {}
        self.pages: set[str] = set()
        self.page_of: dict[str | None, str] = {}
        self.started = False

    @property
    def done(self) -> bool:
        return self.started and not self.pages

    def handle(self, msg: dict) -> None:
        if "id" in msg:
            if reply := self.replies.pop(msg["id"], None):
                reply[0](reply[1], msg.get("result", {}))
            return
        session, params = msg.get("sessionId"), msg.get("params", {})
        method = msg.get("method", "")
        if method == "Target.attachedToTarget":
            self._attached(params)
        elif method == "Target.detachedFromTarget":
            self.pages.discard(params["sessionId"])
        elif method == "Network.requestWillBeSent":
            self._request(session, params)
        elif method == "Network.responseReceived":
            if entry := self.open.get((session, params["requestId"])):
                self._respond(entry, params["response"])
        elif method == "Network.loadingFinished":
            self._finished(session, params["requestId"])

    def _attached(self, params: dict) -> None:
        session = params["sessionId"]
        self.send("Network.enable", BUFFERS, session)
        self.send("Network.setCacheDisabled", {"cacheDisabled": True}, session)
        self.send("Target.setAutoAttach", AUTO_ATTACH, session)
        self.send("Runtime.runIfWaitingForDebugger", {}, session)
        if params["targetInfo"]["type"] == "page":
            self.pages.add(session)
            if not self.started:
                self.started = True
                self.send("Page.navigate", {"url": self.start_url}, session)

    def _request(self, session, params: dict) -> None:
        key = (session, params["requestId"])
        if (redirect := params.get("redirectResponse")) and key in self.open:
            self._respond(self.open[key], redirect)
        request = params["request"]
        url = urlsplit(request["url"])
        if params.get("type") == "Document":
            self.page_of[session] = template(url.path)
        entry = {"request": {"url": request["url"], "method": request["method"],
                             "queryString": [{"name": k, "value": v} for k, v in parse_qsl(url.query)],
                             "postData": {"text": request.get("postData")}},
                 "response": {"status": 0, "content": {"mimeType": ""}},
                 "page": self.page_of.get(session)}
        self.open[key] = entry
        self.entries.append(entry)
        if request.get("hasPostData") and "postData" not in request:
            self._ask("Network.getRequestPostData", params["requestId"], session, self._post_data, entry)

    def _respond(self, entry: dict, response: dict) -> None:
        entry["response"]["status"] = response["status"]
        entry["response"]["content"]["mimeType"] = response.get("mimeType", "")

    def _finished(self, session, request_id: str) -> None:
        entry = self.open.pop((session, request_id), None)
        if entry and any(t in entry["response"]["content"]["mimeType"].lower() for t in API_TYPES):
            self._ask("Network.getResponseBody", request_id, session, self._body, entry)

    def _ask(self, method: str, request_id: str, session, handler, entry: dict) -> None:
        self.replies[self.send(method, {"requestId": request_id}, session)] = (handler, entry)

    @staticmethod
    def _post_data(entry: dict, result: dict) -> None:
        entry["request"]["postData"]["text"] = result.get("postData")

    @staticmethod
    def _body(entry: dict, result: dict) -> None:
        entry["response"]["content"]["text"] = result.get("body")
        if result.get("base64Encoded"):
            entry["response"]["content"]["encoding"] = "base64"


def chrome_path() -> str:
    path = os.environ.get("CHROME") or shutil.which("google-chrome") or MAC_CHROME
    if not Path(path).exists():
        raise SystemExit("Chrome not found. Put its path in the environment variable CHROME.")
    return path


def access(url: str) -> dict:
    """One plain request, with an honest name. Do not retry it and do not change the name."""
    try:
        return {"status": httpx2.get(url, headers={"User-Agent": AGENT}, timeout=20, follow_redirects=True).status_code}
    except httpx2.HTTPError as e:
        return {"error": type(e).__name__}


@contextmanager
def chrome(profile: Path, headless: bool = False) -> Iterator[tuple[subprocess.Popen, str, str]]:
    """Starts Chrome with its own profile. Returns the process, the DevTools port and the websocket path."""
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    port_file = profile / "DevToolsActivePort"
    port_file.unlink(missing_ok=True)
    args = [chrome_path(), f"--user-data-dir={profile}", "--remote-debugging-port=0",
            "--no-first-run", "--no-default-browser-check", "about:blank"]
    process = subprocess.Popen(args + ["--headless=new"] * headless,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 30
        while not port_file.exists() or len(port_file.read_text().split()) < 2:
            if time.time() > deadline or process.poll() is not None:
                raise SystemExit("Chrome does not start.")
            time.sleep(0.1)
        port, path = port_file.read_text().split()
        yield process, port, path
    finally:
        process.terminate()
        process.wait(timeout=10)


def record(url: str, profile: Path, headless: bool = False) -> list[dict]:
    with chrome(profile, headless) as (process, port, path), connect(f"ws://127.0.0.1:{port}{path}", max_size=None) as ws:
        ids = iter(range(1, 10**9))

        def send(method, params=None, session=None) -> int:
            number = next(ids)
            message = {"id": number, "method": method, "params": params or {}}
            ws.send(json.dumps({**message, "sessionId": session} if session else message))
            return number

        recorder = Recorder(send, url)
        send("Target.setDiscoverTargets", {"discover": True})
        send("Target.setAutoAttach", AUTO_ATTACH)
        while not recorder.done:
            try:
                recorder.handle(json.loads(ws.recv(timeout=1)))
            except TimeoutError:
                if process.poll() is not None:
                    break
            except (ConnectionClosed, KeyboardInterrupt):
                break
        return recorder.entries


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m connector.record")
    parser.add_argument("name", help="name of the site, for example bol")
    parser.add_argument("url", help="start page, which robots.txt allows")
    parser.add_argument("--host", help="only this domain; by default the domain of the url")
    parser.add_argument("--headless", action="store_true", help="without a window, for tests")
    args = parser.parse_args()

    host = args.host or urlsplit(args.url).netloc.removeprefix("www.")
    result = {"name": args.name, "url": args.url, "at": datetime.now(UTC).isoformat(),
              "access": access(args.url)}
    print(f"Access without a browser: {result['access']}")
    print("Click what you want to test and close the window.")
    entries = record(args.url, Store(args.name).path("chrome-profile"), args.headless)
    result |= summarize({"log": {"entries": entries}}, host)

    out = Path("recordings") / f"{args.name}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"{len(entries)} requests recorded in {out}")


if __name__ == "__main__":
    main()
