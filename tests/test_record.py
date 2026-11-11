"""Tests for shared/record.py, with faked messages from Chrome."""
from __future__ import annotations

import json

from connector.har import summarize
from connector.record import Recorder


class Chrome:
    """Remembers commands and plays messages back on the recorder."""

    def __init__(self, url="https://shop.invalid/start"):
        self.sent = []
        self.recorder = Recorder(self.send, url)

    def send(self, method, params=None, session=None):
        self.sent.append((method, params, session))
        return len(self.sent)

    def event(self, method, session="page", **params):
        self.recorder.handle({"method": method, "sessionId": session, "params": params})

    def reply(self, method, **result):
        number = next(i for i, sent in enumerate(self.sent, 1) if sent[0] == method)
        self.recorder.handle({"id": number, "result": result})

    def attach(self, session="page", kind="page"):
        self.event("Target.attachedToTarget", session=None, sessionId=session, targetInfo={"type": kind})

    def request(self, id, url, method="GET", session="page", kind="Fetch", **fields):
        self.event("Network.requestWillBeSent", session=session, requestId=id, type=kind,
                   request={"url": url, "method": method, **fields})

    def respond(self, id, status=200, mime="application/json", session="page"):
        self.event("Network.responseReceived", session=session, requestId=id,
                   response={"status": status, "mimeType": mime})
        self.event("Network.loadingFinished", session=session, requestId=id)

    def summary(self):
        return summarize({"log": {"entries": self.recorder.entries}})

    def methods(self):
        return [sent[0] for sent in self.sent]


def test_network_starts_before_the_first_page_loads():
    chrome = Chrome()
    chrome.attach()
    methods = chrome.methods()
    assert methods.index("Network.enable") < methods.index("Page.navigate")
    assert ("Page.navigate", {"url": "https://shop.invalid/start"}, "page") in chrome.sent


def test_only_the_first_page_is_sent_to_the_start_url():
    chrome = Chrome()
    chrome.attach("a")
    chrome.attach("b")
    assert chrome.methods().count("Page.navigate") == 1


def test_a_worker_is_recorded_like_a_page():
    chrome = Chrome()
    chrome.attach()
    chrome.attach("worker", "shared_worker")
    chrome.request("1", "https://shop.invalid/api/update", session="worker")
    chrome.respond("1", session="worker")
    assert ("Network.enable", chrome.sent[0][1], "worker") in chrome.sent
    assert chrome.summary()["api"][0]["path"] == "/api/update"


def test_the_recording_ends_when_the_last_window_closes():
    chrome = Chrome()
    assert not chrome.recorder.done
    chrome.attach("a")
    chrome.attach("b")
    chrome.attach("worker", "shared_worker")
    chrome.event("Target.detachedFromTarget", session=None, sessionId="a")
    assert not chrome.recorder.done
    chrome.event("Target.detachedFromTarget", session=None, sessionId="b")
    assert chrome.recorder.done


def test_bodies_are_kept_across_a_page_change():
    chrome = Chrome()
    chrome.attach()
    chrome.request("1", "https://shop.invalid/graphql", "POST", postData=json.dumps({"operationName": "AddItem"}))
    chrome.respond("1")
    chrome.reply("Network.getResponseBody", body='{"ok": true}')
    chrome.request("2", "https://shop.invalid/basket/", kind="Document")
    chrome.respond("2", mime="text/html")
    group = chrome.summary()["api"][0]
    assert group["operation"] == "AddItem" and group["response"] == {"ok": "bool"}


def test_large_post_bodies_are_fetched():
    chrome = Chrome()
    chrome.attach()
    chrome.request("1", "https://shop.invalid/graphql", "POST", hasPostData=True)
    chrome.reply("Network.getRequestPostData", postData='{"qty": 1}')
    chrome.respond("1")
    assert chrome.summary()["api"][0]["request"] == {"qty": "int"}


def test_html_bodies_are_not_fetched():
    chrome = Chrome()
    chrome.attach()
    chrome.request("1", "https://shop.invalid/page", kind="Document")
    chrome.respond("1", mime="text/html")
    assert "Network.getResponseBody" not in chrome.methods()


def test_a_redirect_keeps_both_steps():
    chrome = Chrome()
    chrome.attach()
    chrome.request("1", "https://shop.invalid/login", "POST")
    chrome.event("Network.requestWillBeSent", requestId="1", type="Fetch",
                 redirectResponse={"status": 302, "mimeType": "application/json"},
                 request={"url": "https://shop.invalid/home", "method": "GET"})
    chrome.respond("1")
    statuses = {g["path"]: g["statuses"] for g in chrome.summary()["api"]}
    assert statuses == {"/login": {302: 1}, "/home": {200: 1}}


def test_requests_know_their_page_and_keep_their_order():
    chrome = Chrome()
    chrome.attach()
    chrome.request("1", "https://shop.invalid/p/123/", kind="Document")
    chrome.respond("1", mime="text/html")
    chrome.request("2", "https://shop.invalid/api/z")
    chrome.respond("2")
    chrome.request("3", "https://shop.invalid/api/a")
    chrome.respond("3")
    api = chrome.summary()["api"]
    assert [g["path"] for g in api] == ["/api/z", "/api/a"]
    assert api[0]["pages"] == ["/p/{id}/"]


def test_summary_of_a_recording_holds_no_values():
    chrome = Chrome()
    chrome.attach()
    chrome.request("1", "https://shop.invalid/login;jsessionid=SECRET?sid=SECRET", "POST",
                   postData="user=SECRET&password=SECRET")
    chrome.respond("1")
    chrome.reply("Network.getResponseBody", body='{"token": "SECRET"}')
    text = json.dumps(chrome.summary())
    assert "SECRET" not in text
    assert all(key in text for key in ("user", "password", "token", "sid"))
