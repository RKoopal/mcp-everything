"""Logging in from Claude with OAuth. The whole way goes through the OAuth client of the MCP SDK, as with Claude Code."""
from __future__ import annotations

import logging
import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from mcp.client.auth import OAuthClientProvider
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

from connector import oauth
from connector.users import Users

CALLBACK = "http://localhost:3118/callback"
METADATA = OAuthClientMetadata(redirect_uris=[CALLBACK], client_name="Claude Code", response_types=["code"],
                               grant_types=["authorization_code", "refresh_token"], token_endpoint_auth_method="none")


class Memory:
    """TokenStorage for the client: what Claude Code puts on disk."""

    def __init__(self):
        self.tokens = self.client = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client

    async def set_client_info(self, client_info):
        self.client = client_info


def browser(world, **headers) -> httpx2.Client:
    """The user's browser. A form from the page itself sends Origin along."""
    return httpx2.Client(headers={"Origin": world["url"], **headers})


def signed_in(world, storage: Memory, login) -> OAuthClientProvider:
    """The client logs in; `login(page)` is what the user does on the login page, and returns the redirect."""
    seen = {}

    async def redirect(url: str) -> None:
        with browser(world) as http:
            to_login = http.get(url)
            assert to_login.status_code == 302
            seen["back"] = login(http, to_login.headers["location"])

    async def callback() -> AuthorizationCodeResult:
        query = parse_qs(urlsplit(seen["back"]).query)
        return AuthorizationCodeResult(code=query["code"][0], state=query["state"][0])

    return OAuthClientProvider(f"{world['url']}/mcp", METADATA, storage, redirect, callback)


def with_token(token: str):
    def login(http, page) -> str:
        answer = http.post(page, data={"token": token})
        assert answer.status_code == 303, answer.text
        return answer.headers["location"]
    return login


async def tools(world, auth) -> set[str]:
    transport = streamable_http_client(f"{world['url']}/mcp", http_client=create_mcp_http_client(auth=auth))
    async with Client(transport) as mcp:
        return {t.name for t in (await mcp.list_tools()).tools}


@pytest.mark.anyio
async def test_claude_logs_in_and_sees_the_tools(world):
    storage = Memory()
    assert "demo_list_items" in await tools(world, signed_in(world, storage, with_token(world["tokens"]["anna"])))
    assert storage.tokens.access_token.startswith("usa_") and storage.tokens.refresh_token.startswith("usr_")
    assert storage.client.client_name == "Claude Code"
    # The access token is anna's: the extension's API knows her too.
    sites = httpx2.get(f"{world['url']}/api/sites", headers={"Authorization": f"Bearer {storage.tokens.access_token}"})
    assert sites.status_code == 200
    # Only the hash is on disk.
    assert storage.tokens.access_token not in (world["data"] / "connector.db").read_bytes().decode("latin-1")


@pytest.mark.anyio
async def test_a_wrong_token_does_not_log_in(world):
    def login(http, page) -> str:
        answer = http.post(page, data={"token": "usc_verzonnen"})
        assert answer.status_code == 400 and "does not know that token" in answer.text
        return with_token(world["tokens"]["bob"])(http, page)  # the request is still open

    storage = Memory()
    await tools(world, signed_in(world, storage, login))
    sites = httpx2.get(f"{world['url']}/api/sites", headers={"Authorization": f"Bearer {storage.tokens.access_token}"})
    assert sites.status_code == 200


def start_login(world, registered: str | None = None, asked: str | None = None) -> tuple[httpx2.Client, str]:
    """Registers a client and starts a login request, as Claude does. Returns the browser and the login page."""
    metadata = METADATA.model_dump(mode="json", exclude_none=True) | ({"scope": registered} if registered else {})
    client = httpx2.post(f"{world['url']}/register", json=metadata).json()
    http = browser(world)
    to_login = http.get(f"{world['url']}/authorize", params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK, "state": "s1",
        "code_challenge": "x" * 43, "code_challenge_method": "S256", "resource": f"{world['url']}/mcp",
        **({"scope": asked} if asked else {})})
    assert to_login.status_code == 302, to_login.text
    return http, to_login.headers["location"]


def test_the_login_page_says_who_asks_and_where_you_go_back_to(world):
    http, page = start_login(world)
    shown = http.get(page)
    assert shown.status_code == 200
    assert "<strong>Claude Code</strong> wants" in shown.text and "<strong>localhost</strong>" in shown.text
    csp = shown.headers["content-security-policy"]
    # After the form comes a redirect to the client: form-action must allow it, or Chrome blocks it.
    assert "form-action 'self' http://localhost:3118;" in csp
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'<script nonce="{nonce}">' in shown.text and 'type: "token"' in shown.text
    assert re.search(r'data-extension="[a-p]{32}"', shown.text)


def test_the_next_login_on_this_browser_is_one_click(world):
    http, page = start_login(world)
    first = http.post(page, data={"token": world["tokens"]["anna"]})
    assert first.status_code == 303
    cookie = first.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Path=/login" in cookie and "SameSite=lax" in cookie

    http.cookies.set(oauth.COOKIE, first.cookies[oauth.COOKIE])
    _, again = start_login(world)
    assert "Continue as anna" in http.get(again).text
    answer = http.post(again, data={"action": "allow"})
    assert answer.status_code == 303 and answer.headers["location"].startswith(f"{CALLBACK}?code=")

    # A new API token (`users add anna`) makes the cookie invalid.
    world["connector"].users.add("anna")
    _, third = start_login(world)
    assert "Continue as" not in http.get(third).text
    assert http.post(third, data={"action": "allow"}).status_code == 400


def test_signing_up_remembers_you_for_the_login(world):
    code = world["connector"].users.invite()
    welcome = browser(world).post(f"{world['url']}/signup/{code}", data={"name": "carla", "password": PASSWORD})
    assert welcome.status_code == 200 and oauth.COOKIE in welcome.cookies
    assert f"claude mcp add --transport http mcp-everything {world['url']}/mcp<" in welcome.text  # without a token
    http, page = start_login(world)
    http.cookies.set(oauth.COOKIE, welcome.cookies[oauth.COOKIE])
    assert "Continue as carla" in http.get(page).text


PASSWORD = "lang-genoeg-wachtwoord"


def sign_up(http: httpx2.Client, page: str, invite: str, name: str = "carla", password: str = PASSWORD
            ) -> httpx2.Response:
    """What "Create account" under "New here?" on the login page sends."""
    return http.post(page, data={"action": "signup", "invite": invite, "name": name, "password": password})


@pytest.mark.anyio
async def test_a_new_user_makes_the_account_on_the_login_page_and_claude_gets_the_tools(world):
    # Claude first: the invite goes into the login page, not into /signup. Nobody copies a token.
    code = world["connector"].users.invite()

    def login(http, page) -> str:
        assert 'name="invite"' in http.get(page).text and 'name="token"' in http.get(page).text
        welcome = sign_up(http, page, f"{world['url']}/signup/{code}", "Carla")  # the whole link works too
        assert welcome.status_code == 200 and "Welcome, carla" in welcome.text
        assert oauth.COOKIE in welcome.cookies and 'href="/extension.zip"' in welcome.text
        # The token goes to the extension (data-token, for the script), and is there once to copy without it.
        token = re.search(r'data-token="(usc_[\w-]+)"', welcome.text).group(1)
        assert f'<code id="token" class="copy">{token}</code>' in welcome.text
        # "Continue" redirects to Claude after the form: form-action has to allow that.
        assert "form-action 'self' http://localhost:3118;" in welcome.headers["content-security-policy"]
        answer = http.post(page, data={"action": "allow"})  # "Continue to Claude Code": the cookie knows carla
        assert answer.status_code == 303, answer.text
        return answer.headers["location"]

    storage = Memory()
    assert "demo_list_items" in await tools(world, signed_in(world, storage, login))
    users = world["connector"].users
    carla = users.granted(storage.tokens.access_token, "access")
    assert carla["name"] == "carla" and users.invited(carla["user"])  # invited: the recording limit holds
    assert not users.open_invite(code, 25)


def test_a_wrong_or_used_up_invite_makes_no_account(world):
    users = world["connector"].users
    used = users.invite()
    users.redeem(used, "dora", 25)
    code = users.invite()
    http, page = start_login(world)
    for invite, name, password, says in (("made-up", "carla", PASSWORD, "This invite no longer works."),
                                         (used, "carla", PASSWORD, "This invite no longer works."),
                                         (f"{world['url']}/signup/{used}", "carla", PASSWORD, "This invite no longer works."),
                                         (code, "anna", PASSWORD, "That name is taken."),
                                         (code, "x", PASSWORD, "Pick a name of 2 to 32 characters"),
                                         (code, "carla", "kort", "Pick a password of at least 10 characters"),
                                         (code, "carla", "", "Pick a password of at least 10 characters")):
        answer = sign_up(http, page, invite, name, password)
        assert answer.status_code == 400 and says in answer.text, invite
        assert oauth.COOKIE not in answer.cookies
        assert f'value="{invite}"' in answer.text  # what was typed stays in the field
    assert {u["name"] for u in users.all()} == {"anna", "bob", "dora"}
    # The request is still open, and the bare code works.
    assert sign_up(http, page, code).status_code == 200
    assert http.post(page, data={"action": "allow"}).status_code == 303


def test_a_sign_up_form_from_another_site_makes_no_account(world):
    users = world["connector"].users
    code = users.invite()
    http, page = start_login(world)
    for headers in ({"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
                    {"Origin": "null", "Sec-Fetch-Site": "cross-site"}, {}):
        answer = httpx2.post(page, data={"action": "signup", "invite": code, "name": "carla", "password": PASSWORD},
                             headers=headers)
        assert answer.status_code == 400 and oauth.COOKIE not in answer.cookies
    assert {u["name"] for u in users.all()} == {"anna", "bob"} and users.open_invite(code, 25)
    assert sign_up(http, page, code).status_code == 200  # from the page itself it works


def test_when_the_request_runs_out_while_installing_the_retry_is_one_click(world, monkeypatch):
    http, page = start_login(world)
    assert sign_up(http, page, world["connector"].users.invite()).status_code == 200
    eleven_minutes_later = time.time() + 660
    with monkeypatch.context() as later:  # for the login requests only
        later.setattr(oauth, "time", SimpleNamespace(time=lambda: eleven_minutes_later))
        for gone in (http.post(page, data={"action": "allow"}), http.get(page)):
            assert gone.status_code == 410 and "remembers you as <strong>carla</strong>" in gone.text
        stranger = httpx2.get(page)  # a browser without the cookie hears nothing about carla
        assert stranger.status_code == 410 and "remembers you" not in stranger.text
    _, again = start_login(world)
    assert "Continue as carla" in http.get(again).text
    assert http.post(again, data={"action": "allow"}).status_code == 303


def test_a_form_from_another_site_does_not_log_in(world):
    http, page = start_login(world)
    for headers in ({"Origin": "https://evil.example"}, {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
                    {"Origin": "null", "Sec-Fetch-Site": "cross-site"}, {}):
        answer = httpx2.post(page, data={"token": world["tokens"]["anna"]}, headers=headers)
        assert answer.status_code == 400 and "location" not in answer.headers
    assert http.post(page, data={"token": world["tokens"]["anna"]}).status_code == 303  # still open


def test_chrome_says_same_origin_even_when_origin_is_null(world):
    # This is how Chrome sent the form when the page had no-referrer: Origin null, Sec-Fetch-Site same-origin.
    _, page = start_login(world)
    answer = httpx2.post(page, data={"token": world["tokens"]["anna"]},
                         headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
    assert answer.status_code == 303


def test_cancel_goes_back_with_access_denied(world):
    http, page = start_login(world)
    answer = http.post(page, data={"action": "deny"})
    assert answer.status_code == 303
    assert parse_qs(urlsplit(answer.headers["location"]).query) == {"error": ["access_denied"], "state": ["s1"]}
    assert http.get(page).status_code == 410  # the request is gone


def test_any_scope_works_because_scopes_mean_nothing_here(world):
    # A client that registers with one scope and asks for another does not trip over it.
    for registered, asked in ((None, "mcp"), ("claudeai", "mcp"), (None, "claudeai"), ("mcp", None)):
        http, page = start_login(world, registered, asked)
        assert http.post(page, data={"token": world["tokens"]["anna"]}).status_code == 303


def test_a_login_request_works_once(world, monkeypatch):
    http, page = start_login(world)
    first = http.post(page, data={"token": world["tokens"]["anna"]})
    assert first.status_code == 303
    http.cookies.set(oauth.COOKIE, first.cookies[oauth.COOKIE])
    # Loaded again after it logged in (the browser goes back, or the app sends it back): say so, and log in nobody.
    for again in (http.get(page), http.post(page, data={"token": world["tokens"]["anna"]})):
        assert again.status_code == 410 and "location" not in again.headers
        assert "You logged in to <strong>Claude Code</strong>" in again.text
        assert "remembers you as <strong>anna</strong>" in again.text and "/extension.zip" in again.text
    assert "remembers you" not in httpx2.get(page).text  # a browser without the cookie hears nothing about anna
    assert "no longer works" in http.get(f"{world['url']}/login/verzonnen").text
    eleven_minutes_later = time.time() + 660
    monkeypatch.setattr(oauth, "time", SimpleNamespace(time=lambda: eleven_minutes_later))
    assert "no longer works" in http.get(page).text


def test_a_pasted_token_offers_the_extension_before_going_back(world):
    http, page = start_login(world)
    pasted = http.post(page, data={"token": world["tokens"]["anna"], "action": "allow"})  # "Log in", or Enter
    assert pasted.status_code == 200 and "Logged in as anna" in pasted.text and "Download the extension" in pasted.text
    assert f'data-token="{world["tokens"]["anna"]}"' in pasted.text  # for the extension, as for a new account
    assert "only once" not in pasted.text  # you had it already
    http.cookies.set(oauth.COOKIE, pasted.cookies[oauth.COOKIE])
    answer = http.post(page, data={"action": "allow"})  # "Continue to Claude Code": the cookie knows anna
    assert answer.status_code == 303 and answer.headers["location"].startswith(f"{CALLBACK}?code=")


def test_a_flood_of_login_requests_pushes_out_the_oldest(world, monkeypatch):
    # Refusing would lock everyone out while the flood lasts. The oldest request makes way.
    monkeypatch.setattr(oauth, "MAX_REQUESTS", 2)
    http, oldest = start_login(world)
    _, middle = start_login(world)
    _, newest = start_login(world)
    assert http.get(oldest).status_code == 410
    assert http.get(middle).status_code == 200 and http.get(newest).status_code == 200


def test_a_huge_registration_is_refused(world):
    huge = METADATA.model_dump(mode="json", exclude_none=True) | {"client_name": "x" * 10_000}
    refused = httpx2.post(f"{world['url']}/register", json=huge)
    assert refused.status_code == 400 and refused.json()["error"] == "invalid_client_metadata"
    _, page = start_login(world)
    assert httpx2.get(page).status_code == 200


def test_a_long_client_name_is_cut_on_the_page(world):
    long = METADATA.model_dump(mode="json", exclude_none=True) | {"client_name": "Claude " + "veilig " * 100}
    client = httpx2.post(f"{world['url']}/register", json=long).json()
    http = browser(world)
    to_login = http.get(f"{world['url']}/authorize", params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK,
        "code_challenge": "x" * 43, "code_challenge_method": "S256"})
    shown = http.get(to_login.headers["location"]).text
    name = re.search(r"<strong>([^<]*)</strong> wants", shown).group(1)
    assert len(name) == 60


def test_mcp_tells_the_client_where_to_log_in(world):
    unauthorized = httpx2.post(f"{world['url']}/mcp", json={})
    assert unauthorized.status_code == 401
    metadata_url = f"{world['url']}/.well-known/oauth-protected-resource/mcp"
    assert f'resource_metadata="{metadata_url}"' in unauthorized.headers["www-authenticate"]
    for url in (metadata_url, f"{world['url']}/.well-known/oauth-protected-resource"):
        resource = httpx2.get(url).json()
        assert resource["resource"] == f"{world['url']}/mcp"
        assert resource["authorization_servers"] == [world["url"]]  # without a trailing /, just like the issuer
    server = httpx2.get(f"{world['url']}/.well-known/oauth-authorization-server").json()
    assert server["issuer"] == world["url"]
    assert server["registration_endpoint"] == f"{world['url']}/register"
    assert server["code_challenge_methods_supported"] == ["S256"]


@pytest.mark.anyio
async def test_logging_out_from_claude_ends_the_login(world):
    # The way a client without a secret (Claude Code) revokes: only client_id and the token.
    storage = Memory()
    await tools(world, signed_in(world, storage, with_token(world["tokens"]["anna"])))
    access = {"Authorization": f"Bearer {storage.tokens.access_token}"}
    assert httpx2.get(f"{world['url']}/api/sites", headers=access).status_code == 200
    revoked = httpx2.post(f"{world['url']}/revoke", data={"token": storage.tokens.refresh_token,
                                                          "client_id": storage.client.client_id})
    assert revoked.status_code == 200, revoked.text
    assert httpx2.get(f"{world['url']}/api/sites", headers=access).status_code == 401


def test_refresh_tokens_rotate_and_revoking_ends_the_login(tmp_path):
    users = Users(tmp_path)
    user = users.by_token(users.add("anna"))
    users.add_client("c1", "{}")
    access, refresh = users.grant("c1", user["id"], ["mcp"], None, 60, 600)
    assert users.granted(access, "access")["name"] == "anna"
    assert users.granted(access, "refresh") is None and users.granted(refresh, "access") is None

    new_access, new_refresh = users.rotate(refresh, ["mcp"], 60, 600)
    assert users.rotate(refresh, ["mcp"], 60, 600) is None  # a refresh token works once
    assert users.granted(access, "access") is None  # nor does the old access token
    assert users.granted(new_access, "access")["user"] == user["id"]

    users.revoke(new_refresh)
    assert users.granted(new_access, "access") is None and users.granted(new_refresh, "refresh") is None


def test_expired_tokens_do_not_work(tmp_path):
    users = Users(tmp_path)
    user = users.by_token(users.add("anna"))
    users.add_client("c1", "{}")
    access, refresh = users.grant("c1", user["id"], [], None, -1, -1)
    assert users.granted(access, "access") is None and users.rotate(refresh, [], 60, 600) is None


def test_only_the_newest_unused_clients_stay(tmp_path):
    users = Users(tmp_path)
    user = users.by_token(users.add("anna"))
    users.add_client("used", "{}")
    users.grant("used", user["id"], [], None, 60, 600)
    users.revoke(users.grant("used", user["id"], [], None, 60, 600)[0])  # it stays, also after logging out
    for n in range(4):
        users.add_client(f"c{n}", "{}", keep=2)
    assert [users.client(f"c{n}") for n in range(4)] == [None, None, "{}", "{}"]
    assert users.client("used") == "{}"


def test_a_new_api_token_ends_every_oauth_login(tmp_path):
    # Whoever stole the old token and logged in with it through OAuth loses that login too.
    users = Users(tmp_path)
    anna, bob = (users.by_token(users.add(name)) for name in ("anna", "bob"))
    users.add_client("c1", "{}")
    access, refresh = users.grant("c1", anna["id"], [], None, 60, 600)
    bobs, _ = users.grant("c1", bob["id"], [], None, 60, 600)
    users.add("anna")
    assert users.granted(access, "access") is None and users.rotate(refresh, [], 60, 600) is None
    assert users.granted(bobs, "access")["name"] == "bob"


def test_logging_in_with_name_and_password(world):
    # Nobody remembers a token: who signed up logs in with a name and password, also in a browser without the cookie.
    http, page = start_login(world)
    assert sign_up(http, page, world["connector"].users.invite(), "carla").status_code == 200
    browser_without_cookie, page = start_login(world)
    shown = browser_without_cookie.get(page).text
    assert 'name="user"' in shown and 'autocomplete="current-password"' in shown
    assert shown.index("Have an account?") < shown.index("New here?")  # who has an account sees the login first
    for who, password in (("carla", "verkeerd-wachtwoord"), ("niemand", PASSWORD), ("carla", "")):
        wrong = browser_without_cookie.post(page, data={"user": who, "password": password, "action": "allow"})
        assert wrong.status_code == 400 and oauth.COOKIE not in wrong.cookies
        assert ("do not match" if password else "Log in with your name and password") in wrong.text
        assert f'value="{who}"' in wrong.text  # the name stays in the field
    right = browser_without_cookie.post(page, data={"user": "Carla ", "password": PASSWORD, "action": "allow"})
    assert right.status_code == 303 and right.headers["location"].startswith(f"{CALLBACK}?code=")
    assert oauth.COOKIE in right.cookies  # and the next time it is one click


def test_five_wrong_passwords_lock_the_name_and_not_the_token(world):
    world["connector"].users.set_password("anna", PASSWORD)
    http, page = start_login(world)
    for _ in range(5):
        assert "do not match" in http.post(page, data={"user": "anna", "password": "geraden-wachtwoord"}).text
    locked = http.post(page, data={"user": "anna", "password": PASSWORD, "action": "allow"})
    assert locked.status_code == 400 and "Too many wrong passwords" in locked.text
    world["connector"].users.set_password("bob", PASSWORD)  # another name is not locked
    assert http.post(page, data={"user": "bob", "password": PASSWORD}).status_code == 303
    _, page = start_login(world)
    assert http.post(page, data={"token": world["tokens"]["anna"]}).status_code == 303  # the token still works


def test_a_login_with_a_token_does_not_reset_the_count_of_another_name(world):
    # Otherwise anyone with an account guesses without limit: four wrong, a login of their own with "user=anna", again.
    world["connector"].users.set_password("anna", PASSWORD)
    http, page = start_login(world)
    for _ in range(5):
        http.post(page, data={"user": "anna", "password": "geraden-wachtwoord"})
    assert http.post(page, data={"user": "anna", "token": world["tokens"]["bob"], "action": "allow"}).status_code == 200
    locked = http.post(page, data={"user": "anna", "password": PASSWORD, "action": "allow"})
    assert locked.status_code == 400 and "Too many wrong passwords" in locked.text


def test_a_new_password_logs_out_everywhere(world):
    # The way to fix a stolen password: the remembered login and every OAuth login of that user stop working.
    connector = world["connector"]
    users = connector.users
    anna = users.by_token(world["tokens"]["anna"])
    provider = oauth.OAuth(users, world["url"], connector.settings.key)
    cookie = provider.cookie(anna["id"])
    users.add_client("c1", "{}")
    access, refresh = users.grant("c1", anna["id"], [], None, 60, 600)
    assert provider.from_cookie(cookie)["name"] == "anna"
    assert users.set_password("anna", PASSWORD)
    assert provider.from_cookie(cookie) is None
    assert users.granted(access, "access") is None and users.rotate(refresh, [], 60, 600) is None
    assert provider.from_cookie(provider.cookie(anna["id"]))["name"] == "anna"  # a new login works
    assert provider.from_cookie(f"{anna['id']}.\u00b2\u00b2.x") is None  # no 500 on a strange cookie


def test_each_login_attempt_is_logged_with_the_address_from_caddy(world, caplog):
    # To see afterwards whether someone guessed passwords. Behind Caddy the real address is in X-Forwarded-For.
    caplog.set_level(logging.INFO, logger="connector.login")
    world["connector"].users.set_password("anna", PASSWORD)
    http, page = start_login(world)
    http.headers["X-Forwarded-For"] = "203.0.113.7"
    for _ in range(5):
        http.post(page, data={"user": "anna", "password": "geraden-wachtwoord"})
    http.post(page, data={"user": "anna", "password": PASSWORD})
    http.post(page, data={"token": "usc_onzin"})
    assert http.post(page, data={"token": world["tokens"]["bob"]}).status_code == 303
    lines = [r.getMessage() for r in caplog.records if r.name == "connector.login"]
    assert [line.split(" name=")[0] for line in lines] == ["wrong password"] * 5 + ["locked", "wrong token", "ok, token"]
    assert all("ip=203.0.113.7" in line for line in lines) and "name='anna'" in lines[0] and "name='bob'" in lines[-1]
    assert "geraden" not in " ".join(lines)  # never the password
