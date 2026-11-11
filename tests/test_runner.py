"""The runner and the kinds of login. All HTTP is fake."""
from __future__ import annotations

import json
import threading
import time

import httpx2
import pytest

from connector.auth import CREDENTIALS, SESSION, TOKENS
from connector.errors import AuthExpired, ConnectorError, StateUnknown, Unverified
from connector.runner import Runner, guard
from connector.store import Store
from tests.helpers import BASE, Site, manifest, ok, tool

GET = tool("get", input_schema={"type": "object", "properties": {"id": {"type": "string"}, "q": {"type": "string"},
                                                                   "n": {"type": "integer", "default": 5}},
                                "required": ["id"]},
           request={"method": "GET", "path": "/items/{id}", "query": {"q": "{q}", "n": "{n}"}})
ADD = tool("add", "write", human_only=False, reversible="remove", verifier={"path": "id"},
           request={"method": "POST", "path": "/items", "json": {"name": "{name}"}},
           input_schema={"type": "object", "properties": {"name": {"type": "string"}}})
REMOVE = tool("remove", "write", reversible="add")
PRIVATE = tool("buy", "write")
LOGIN = {"type": "password", "fields": ["email", "password"],
         "login": {"method": "POST", "path": "/login", "json": {"email": "{email}", "password": "{password}"}}}
OAUTH = {"type": "oauth", "authorize_url": "https://login.site.invalid/authorize",
         "token": {"method": "POST", "path": "/token", "json": {"code": "{code}"}},
         "refresh": {"method": "POST", "path": "/refresh", "json": {"refresh_token": "{refresh_token}"}}}


@pytest.fixture
def store(tmp_path):
    return Store("site", tmp_path)


SESSION_AUTH = {"type": "session"}


def run(store, site: Site, **fields) -> Runner:
    return Runner(manifest(GET, ADD, REMOVE, PRIVATE, **fields), store, site.client(), sleep=lambda _: None)


# ---------- reading ----------

def test_read_fills_path_query_and_defaults(store):
    site = Site(ok({"a": 1}))
    assert run(store, site).call("get", {"id": "a/b c"}) == {"a": 1}
    assert str(site.seen[0].url) == f"{BASE}/items/a%2Fb%20c?n=5"


def test_read_sends_the_session_unless_the_tool_says_not_to(store):
    store.write(SESSION, {"cookies": {"sid": "s"}})
    site = Site(ok())
    runner = Runner(manifest(GET, tool("search", session=False), auth=SESSION_AUTH), store, site.client())
    runner.call("get", {"id": "1"})
    runner.call("search")
    assert site.seen[0].headers["cookie"] == "sid=s" and "cookie" not in site.seen[1].headers


def test_an_anonymous_answer_never_becomes_the_session(store):
    site = Site(ok(headers=[("set-cookie", "sid=anoniem; Path=/")]))
    runner = Runner(manifest(GET, tool("search", session=False), auth=SESSION_AUTH), store, site.client())
    runner.call("search")
    assert not runner.auth.connected()
    store.write(SESSION, {"cookies": {"sid": "ingelogd"}})
    runner.call("search")
    assert store.read(SESSION)["cookies"] == {"sid": "ingelogd"}


def test_nothing_goes_out_before_the_user_connects(store):
    site = Site(ok())
    runner = Runner(manifest(GET, tool("search", session=False), auth=SESSION_AUTH), store, site.client())
    with pytest.raises(AuthExpired, match="not linked"):
        runner.call("get", {"id": "1"})
    runner.call("search")
    assert len(site.seen) == 1


def test_read_returns_text_for_html(store):
    assert run(store, Site(httpx2.Response(200, text="<p>hoi</p>"))).call("get", {"id": "1"}) == "<p>hoi</p>"


def test_read_retries_transient_failures(store):
    site = Site(ok(status=503), httpx2.ConnectError("weg"), ok({"a": 1}))
    assert run(store, site).call("get", {"id": "1"}) == {"a": 1}
    assert len(site.seen) == 3


def test_read_gives_up_after_the_limit(store):
    site = Site(ok(status=503))
    with pytest.raises(ConnectorError, match="503"):
        run(store, site).call("get", {"id": "1"})
    assert len(site.seen) == 3


def test_a_site_can_ask_for_a_single_attempt(store):
    site = Site(ok(status=429))
    with pytest.raises(ConnectorError):
        run(store, site, attempts=1).call("get", {"id": "1"})
    assert len(site.seen) == 1


def test_client_error_is_not_retried_and_never_shows_the_url(store):
    site = Site(ok(status=404))
    with pytest.raises(ConnectorError) as error:
        run(store, site).call("get", {"id": "geheim"})
    assert len(site.seen) == 1 and "site.invalid" not in str(error.value) and "geheim" not in str(error.value)


def test_call_refuses_a_write(store):
    with pytest.raises(ConnectorError, match="is a write"):
        run(store, Site(ok())).call("add", {})


# ---------- session ----------

def test_session_without_renewal_stops_at_the_first_refusal(store):
    site = Site(ok(status=401))
    with pytest.raises(AuthExpired):
        run(store, site).call("get", {"id": "1"})
    assert len(site.seen) == 1


def test_session_keeps_rotated_cookies_but_never_bot_cookies(store):
    store.write(SESSION, {"cookies": {"sid": "oud", "taal": "nl"}})
    site = Site(ok(headers=[("set-cookie", "sid=nieuw; Path=/"), ("set-cookie", "_abck=bot; Path=/")]), ok())
    runner = run(store, site, auth=SESSION_AUTH)
    runner.call("get", {"id": "1"})
    runner.call("get", {"id": "1"})
    assert store.read(SESSION)["cookies"] == {"sid": "nieuw", "taal": "nl"}
    assert site.seen[1].headers["cookie"] == "sid=nieuw; taal=nl"


def test_password_logs_in_again_once(store):
    store.write(CREDENTIALS, {"email": "a@b.nl", "password": "pw"})
    site = Site(ok(status=401), ok(headers={"set-cookie": "sid=vers"}), ok({"a": 1}))
    assert run(store, site, auth=LOGIN).call("get", {"id": "1"}) == {"a": 1}
    assert json.loads(site.seen[1].content) == {"email": "a@b.nl", "password": "pw"}
    assert site.seen[2].headers["cookie"] == "sid=vers"


def test_password_login_is_never_retried(store):
    store.write(CREDENTIALS, {"email": "a@b.nl", "password": "fout"})
    store.write(SESSION, {"cookies": {"sid": "oud"}})
    site = Site(ok(status=401), ok(status=401))
    with pytest.raises(AuthExpired):
        run(store, site, auth=LOGIN).call("get", {"id": "1"})
    assert len(site.seen) == 2
    # Not on the next call either: a refused password is forgotten, until a person links again.
    assert store.read(CREDENTIALS) is None and store.read(SESSION) is None
    with pytest.raises(AuthExpired):
        run(store, site, auth=LOGIN).call("get", {"id": "1"})
    assert len(site.seen) == 2


@pytest.mark.parametrize("status", [503, 429, 408])
def test_a_password_stays_when_the_site_is_down_or_busy(store, status):
    # 429: too many logins in a short time. The password is still right; only a refused login wipes it.
    store.write(CREDENTIALS, {"email": "a@b.nl", "password": "pw"})
    with pytest.raises((AuthExpired, ConnectorError)):
        run(store, Site(ok(status=401), ok(status=status)), auth=LOGIN).call("get", {"id": "1"})
    assert store.read(CREDENTIALS) == {"email": "a@b.nl", "password": "pw"}


def test_two_refusals_at_once_log_in_once(store):
    store.write(CREDENTIALS, {"email": "a@b.nl", "password": "pw"})
    store.write(SESSION, {"cookies": {"sid": "oud"}})

    def slow(request):
        time.sleep(0.2)
        return ok(headers={"set-cookie": "sid=vers"})

    site = Site(slow)
    threads = [threading.Thread(target=run(store, site, auth=LOGIN).auth.renew) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(site.seen) == 1 and store.read(SESSION)["cookies"] == {"sid": "vers"}


def test_oauth_refreshes_an_expired_token_before_use(store):
    store.write(TOKENS, {"access_token": "oud", "refresh_token": "r1", "expires_at": 0})
    site = Site(ok({"access_token": "nieuw", "refresh_token": "r2", "expires_in": 3600}), ok({"a": 1}))
    run(store, site, auth=OAUTH).call("get", {"id": "1"})
    assert site.seen[1].headers["authorization"] == "Bearer nieuw"
    assert store.read(TOKENS)["refresh_token"] == "r2" and store.read(TOKENS)["expires_at"] > time.time()


def test_oauth_refreshes_once_when_two_calls_need_it(store):
    """The refresh token works once: a second refresh with the same token would break the session."""
    store.write(TOKENS, {"access_token": "a", "refresh_token": "r1", "expires_at": time.time() + 999})

    def slow(request):
        time.sleep(0.2)
        return ok({"access_token": "b", "refresh_token": "r2"})

    site = Site(slow)
    threads = [threading.Thread(target=run(store, site, auth=OAUTH).auth.renew) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(site.seen) == 1 and store.read(TOKENS)["refresh_token"] == "r2"


def test_connect_stores_what_the_site_accepts(store):
    site = Site(ok(headers={"set-cookie": "sid=s"}))
    runner = run(store, site, auth=LOGIN)
    assert runner.auth.connect({"email": "a@b.nl", "password": "pw"})
    assert store.read(SESSION)["cookies"] == {"sid": "s"} and store.read(CREDENTIALS)["password"] == "pw"


def test_connect_stores_nothing_the_site_refuses(store):
    assert not run(store, Site(ok(status=401)), auth=LOGIN).auth.connect({"email": "a", "password": "b"})
    assert store.read(CREDENTIALS) is None and store.read(SESSION) is None


def test_an_oauth_answer_without_tokens_connects_nothing(store):
    assert not run(store, Site(ok({"error": "nee"})), auth=OAUTH).auth.connect("code")
    assert store.read(TOKENS) is None


# ---------- writing ----------

def test_write_happens_once_per_key(store):
    site = Site(ok({"id": "n1"}))
    runner = run(store, site)
    assert runner.write("add", {"name": "x"}, "k1") == {"id": "n1"}
    assert runner.write("add", {"name": "x"}, "k1") == {"id": "n1"}
    assert len(site.seen) == 1


def test_write_needs_write(store):
    with pytest.raises(ConnectorError, match="not a write"):
        run(store, Site(ok())).write("get", {"id": "1"}, "k")


@pytest.mark.parametrize("answer", [httpx2.ReadTimeout("weg"), ok(status=502)])
def test_unknown_outcome_is_not_retried_and_blocks_the_next_attempt(store, answer):
    site = Site(answer)
    runner = run(store, site)
    with pytest.raises(StateUnknown):
        runner.write("add", {"name": "x"}, "k1")
    with pytest.raises(StateUnknown, match="Check it on the site"):
        runner.write("add", {"name": "x"}, "k1")
    assert len(site.seen) == 1


def test_refused_write_may_be_tried_again(store):
    site = Site(ok(status=422), ok({"id": "n1"}))
    runner = run(store, site)
    with pytest.raises(ConnectorError):
        runner.write("add", {"name": "x"}, "k1")
    assert runner.write("add", {"name": "x"}, "k1") == {"id": "n1"}


def test_silent_failure_is_loud_and_blocks_a_repeat(store):
    site = Site(ok({"id": None}))
    runner = run(store, site)
    with pytest.raises(Unverified):
        runner.write("add", {"name": "x"}, "k1")
    with pytest.raises(StateUnknown):
        runner.write("add", {"name": "x"}, "k1")


def test_write_may_renew_the_session_because_a_refusal_did_nothing(store):
    store.write(CREDENTIALS, {"email": "a", "password": "b"})
    site = Site(ok(status=401), ok(headers={"set-cookie": "sid=s"}), ok({"id": "n1"}))
    assert run(store, site, auth=LOGIN).write("add", {"name": "x"}, "k1") == {"id": "n1"}


def test_human_only_needs_the_users_confirmation(store):
    site = Site(ok())
    runner = run(store, site)
    with pytest.raises(ConnectorError, match="confirm"):
        runner.write("buy", {}, "k1")
    assert site.seen == []
    runner.write("buy", {}, "k1", confirmed=True)
    assert len(site.seen) == 1


def test_ledger_holds_no_request_body(store):
    run(store, Site(ok({"id": "n1"}))).write("add", {"name": "geheim"}, "k1")
    assert "geheim" not in store.path("ledger.jsonl").read_text()


# ---------- only the site itself ----------

@pytest.mark.parametrize("url", ["https://ander.invalid/x", "http://site.invalid/x", "https://site.invalid:8443/x"])
def test_guard_refuses_other_sites(url):
    with pytest.raises(ConnectorError, match="outside the site"):
        guard(BASE)(url)


def test_guard_refuses_private_addresses_on_a_server():
    with pytest.raises(ConnectorError, match="public addresses"):
        guard("https://localhost")("https://localhost/x")
    guard("http://localhost", allow_private=True)("http://localhost/x")


def test_cookies_from_two_calls_at_once_are_both_kept(store):
    from connector.auth import SessionAuth

    store.write(SESSION, {"cookies": {"sid": "s"}})
    auth = SessionAuth(store)
    responses = [httpx2.Response(200, headers={"set-cookie": f"c{n}=v"}, request=httpx2.Request("GET", BASE))
                 for n in range(16)]
    threads = [threading.Thread(target=auth.seen, args=(r,)) for r in responses]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.read(SESSION)["cookies"] == {"sid": "s"} | {f"c{n}": "v" for n in range(16)}
