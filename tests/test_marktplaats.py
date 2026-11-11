"""The Marktplaats tools from sites/marktplaats/tools.json. All HTTP is fake; the shape of the page comes from the
built-in browser."""
from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest

from connector.auth import SESSION, SessionAuth
from connector.errors import AuthExpired, ConnectorError
from connector.manifest import load
from connector.runner import Runner
from connector.store import Store
from tests.helpers import Site, ok

MANIFEST = load(Path(__file__).resolve().parent.parent / "sites/marktplaats/tools.json")
LISTING = {"itemId": "m1", "title": "Fujifilm X-T30", "vipUrl": "/v/audio-tv-en-foto/m1-fujifilm",
           "priceInfo": {"priceCents": 65000, "priceType": "FIXED"}, "location": {"cityName": "Utrecht"},
           "date": "Vandaag", "reserved": False, "sellerInformation": {"sellerId": 7, "sellerName": "Verkoper"}}
PAGE = {"props": {"pageProps": {"searchRequestAndResponse": {"listings": [LISTING]}}}}
HTML = f'<script id="__NEXT_DATA__" type="application/json" crossorigin="anonymous">{json.dumps(PAGE)}</script>'


@pytest.fixture
def store(tmp_path):
    store = Store("marktplaats", tmp_path)
    SessionAuth(store).connect({"cookies": {"MpSession": "s", "aws-waf-token": "w", "MpSecurity": "t=="}})
    return store


def run(store, site: Site) -> Runner:
    return Runner(MANIFEST, store, site.client(), sleep=lambda _: None)


def test_has_no_writes():
    assert {t["effect"] for t in MANIFEST["tools"]} == {"read"}


def test_search_normalizes_and_drops_the_seller(store):
    site = Site(httpx2.Response(200, text=HTML))
    [item] = run(store, site).call("search", {"query": "fujifilm xt30"})
    assert site.seen[0].url.path == "/q/fujifilm xt30/"
    assert item == {"id": "m1", "title": "Fujifilm X-T30", "price_cents": 65000, "price_type": "FIXED",
                    "city": "Utrecht", "date": "Vandaag", "reserved": False, "url": "/v/audio-tv-en-foto/m1-fujifilm"}


def test_search_sends_no_session_even_if_one_is_stored(store):
    site = Site(httpx2.Response(200, text=HTML))
    run(store, site).call("search", {"query": "x"})
    assert "cookie" not in site.seen[0].headers


def test_a_challenge_page_is_an_error_not_an_empty_result(store):
    with pytest.raises(ConnectorError, match="blocked"):
        run(store, Site(httpx2.Response(202, text="<html>verify</html>"))).call("search", {"query": "x"})


@pytest.mark.parametrize("status", [429, 503])
def test_a_refusal_is_not_retried(store, status):
    site = Site(ok(status=status))
    with pytest.raises(ConnectorError):
        run(store, site).call("search", {"query": "x"})
    assert len(site.seen) == 1


def test_a_403_asks_for_a_human_without_retrying(store):
    site = Site(ok(status=403))
    with pytest.raises(AuthExpired):
        run(store, site).call("message_count")
    assert len(site.seen) == 1


def test_the_session_never_holds_bot_protection(store):
    site = Site(ok({"count": 1}))
    run(store, site).call("message_count")
    assert site.seen[0].headers["cookie"] == "MpSession=s; MpSecurity=t=="
    assert "aws-waf-token" not in store.read(SESSION)["cookies"]


def test_conversations_send_the_recorded_input_with_defaults(store):
    site = Site(ok())
    run(store, site).call("get_conversations", {"limit": 5})
    assert site.seen[0].url.path.endswith("/messages/api/rpc/conversations.getConversations")
    assert site.seen[0].url.params["input"] == '{"json":{"limit":5,"offset":0}}'


def test_messages_use_the_conversation_id(store):
    site = Site(ok())
    run(store, site).call("get_messages", {"conversation_id": "gesprek:1"})
    assert site.seen[0].url.params["input"] == '{"json":{"conversationId":"gesprek:1"}}'
