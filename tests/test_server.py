"""The server from start to end: real HTTP, a demo site with a login, and the MCP client of the SDK."""
from __future__ import annotations

import io
import json
import re
import threading
import time
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import httpx2
import mcp_types as types
import pytest
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from connector.server import _confirm, extension_id, mcp_server
from tests.conftest import stub_model
from tests.helpers import PASSWORD

MODES = ["legacy", "2026-07-28"]
SAME_SITE = {"Sec-Fetch-Site": "same-origin"}  # what a browser sends with a form from the page itself


def bearer(world, user="anna") -> dict:
    return {"Authorization": f"Bearer {world['tokens'][user]}"}


def client(world, user="anna", mode="legacy", answer=None) -> Client:
    async def elicit(context, params):
        return answer

    transport = streamable_http_client(f"{world['url']}/mcp", http_client=create_mcp_http_client(headers=bearer(world, user)))
    return Client(transport, mode=mode, elicitation_callback=elicit if answer else None)


async def call(world, name, args=None, **kwargs) -> types.CallToolResult:
    async with client(world, **kwargs) as mcp:
        return await mcp.call_tool(name, args or {})


def text(result: types.CallToolResult) -> str:
    return result.content[0].text


def extract(world, recording, headers=None) -> dict:
    """Like the extension: send the recording, then ask for the state until the server is done."""
    headers = headers or bearer(world)
    started = httpx2.post(world["url"] + "/api/recordings", json=recording, headers=headers)
    assert started.status_code == 202, started.text
    for _ in range(200):
        job = httpx2.get(f"{world['url']}/api/recordings/{started.json()['job']}", headers=headers).json()
        if job["state"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("the server did not get ready")


def recording_of(world, host="127.0.0.1") -> dict:
    site = world["site"].replace("127.0.0.1", host)  # to the server, localhost is another, new site
    return {"host": host, "log": {"entries": [{
        "request": {"method": "GET", "url": f"{site}/api/profile?lang=nl",
                    "headers": [{"name": "Cookie", "value": "sid=sessie-anna"}]},
        "response": {"status": 200, "content": {"mimeType": "application/json", "text": '{"name": "Anna"}'}}}]}}


def connect(world, user="anna", password=PASSWORD) -> httpx2.Response:
    link = world["connector"].how_to_connect({"id": world["connector"].users.by_token(world["tokens"][user])["id"],
                                              "name": user}, "demo").rsplit(" ", 1)[1]
    return httpx2.post(link, data={"email": "anna@demo.invalid", "password": password})


def test_claude_can_give_the_user_the_extension(world, monkeypatch):
    url = world["connector"].settings.public_url
    assert f"{url}/extension.zip" in mcp_server(world["connector"]).instructions
    monkeypatch.setattr(world["connector"], "manifest", lambda site: {"auth": {"type": "session"}, "base": "https://x.example"})
    said = world["connector"].how_to_connect({"id": "u1", "name": "anna"}, "x")
    assert "Link session" in said and f"{url}/extension.zip" in said


def test_everything_needs_a_token(world):
    for path in ("/mcp", "/api/sites"):
        assert httpx2.post(world["url"] + path, json={}).status_code in (401, 405)
        assert httpx2.get(world["url"] + path, headers={"Authorization": "Bearer usc_fout"}).status_code == 401
    assert httpx2.get(world["url"] + "/healthz").json() == {"ok": True}


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_tools_of_every_site_are_listed(world, mode):
    async with client(world, mode=mode) as mcp:
        tools = {t.name: t for t in (await mcp.list_tools()).tools}
    assert set(tools) == {"sites", "connect", "demo_list_items", "demo_add_item"}
    assert tools["demo_list_items"].annotations.read_only_hint is True
    assert "idempotency_key" in tools["demo_add_item"].input_schema["required"]


@pytest.mark.anyio
async def test_connecting_with_a_password_through_a_one_time_link(world):
    result = await call(world, "connect", {"site": "demo"})
    link = text(result).rsplit(" ", 1)[1]
    page = httpx2.get(link)
    assert page.status_code == 200 and 'type="password"' in page.text
    # Whose link this is: someone can send you their own, and then your password would land in their vault.
    assert "to <strong>anna</strong> on MCP Everything" in page.text and "Not your name?" in page.text
    assert page.headers["referrer-policy"] == "same-origin" and "default-src 'none'" in page.headers["content-security-policy"]
    wrong = httpx2.post(link, data={"email": "anna@demo.invalid", "password": "fout"})
    assert wrong.status_code == 400 and "Logging in failed" in wrong.text
    assert "Linked" in httpx2.post(link, data={"email": "anna@demo.invalid", "password": PASSWORD}).text
    assert httpx2.get(link).status_code == 410
    stored = next((world["data"] / "vault").glob("*/demo/credentials.json"))
    assert PASSWORD.encode() not in stored.read_bytes()


@pytest.mark.anyio
async def test_reading_after_connecting_and_not_before(world):
    before = await call(world, "demo_list_items")
    assert before.is_error and "connect" in text(before)
    connect(world)
    after = await call(world, "demo_list_items")
    assert not after.is_error and json.loads(text(after)) == []


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_a_human_only_write_waits_for_the_user(world, mode):
    connect(world)
    args = {"name": "melk", "idempotency_key": "boodschap-1"}
    declined = await call(world, "demo_add_item", args, mode=mode, answer=types.ElicitResult(action="decline"))
    assert declined.is_error and "Not done" in text(declined)
    accept = types.ElicitResult(action="accept", content={"confirm": True})
    first = await call(world, "demo_add_item", args, mode=mode, answer=accept)
    again = await call(world, "demo_add_item", args, mode=mode, answer=accept)
    assert json.loads(text(first)) == json.loads(text(again)) == {"item": {"id": "i1", "name": "melk"}}


@pytest.mark.anyio
async def test_a_yes_counts_only_for_the_call_that_was_shown():
    class Session:
        def check_client_capability(self, capability):
            return True

    ctx = SimpleNamespace(session=Session(), protocol_version="2026-07-28")
    accept = types.ElicitResult(action="accept", content={"confirm": True})

    def params(state):
        return types.CallToolRequestParams(name="demo_add_item", arguments={}, input_responses={"confirm": accept},
                                           request_state=state)

    assert isinstance(await _confirm(ctx, params(None), "Toevoegen?", "zegel"), types.InputRequiredResult)
    assert isinstance(await _confirm(ctx, params("andere"), "Toevoegen?", "zegel"), types.InputRequiredResult)
    assert await _confirm(ctx, params("zegel"), "Toevoegen?", "zegel") is True


@pytest.mark.anyio
async def test_a_client_that_cannot_ask_the_user_sends_nothing(world):
    connect(world)
    result = await call(world, "demo_add_item", {"name": "melk", "idempotency_key": "boodschap-1"})
    assert result.is_error and "Nothing was sent" in text(result)


@pytest.mark.anyio
async def test_invalid_input_is_refused_before_anything_is_sent(world):
    connect(world)
    result = await call(world, "demo_add_item", {"name": 3})
    assert result.is_error and "Invalid input" in text(result)


@pytest.mark.anyio
async def test_what_anna_records_bob_can_use_but_only_with_his_own_session(world):
    job = extract(world, recording_of(world))
    assert job == {"state": "done", "step": "Checking and saving tools", "host": "127.0.0.1",
                   "result": {"site": "demo", "new_site": False, "added": ["demo_profile"], "rejected": [],
                              "session": True, "shared": True}}
    anna = await call(world, "demo_profile", {"lang": "en"})
    assert json.loads(text(anna)) == {"name": "Anna", "lang": "en"}
    bob = await call(world, "demo_profile", {"lang": "en"}, user="bob")
    assert bob.is_error and "connect" in text(bob)


@pytest.mark.anyio
async def test_what_a_guest_records_everyone_can_use_while_the_review_is_off(world):
    connector = world["connector"]
    token = connector.users.redeem(connector.users.invite(), "carla", 10)
    job = extract(world, recording_of(world), {"Authorization": f"Bearer {token}"})
    assert job["result"]["added"] == ["demo_profile"] and job["result"]["shared"] is True
    async with client(world) as mcp:
        assert "demo_profile" in {t.name for t in (await mcp.list_tools()).tools}
    assert not connector.registry.pending()


@pytest.mark.anyio
async def test_with_the_review_on_others_see_what_a_guest_records_only_after_a_review(world):
    connector = world["connector"]
    connector.settings = replace(connector.settings, review_guest_tools=True)
    token = connector.users.redeem(connector.users.invite(), "carla", 10)
    world["tokens"]["carla"] = token
    job = extract(world, recording_of(world), {"Authorization": f"Bearer {token}"})
    assert job["result"]["added"] == ["demo_profile"] and job["result"]["shared"] is False
    async with client(world, user="carla") as mcp:
        assert "demo_profile" in {t.name for t in (await mcp.list_tools()).tools}
    async with client(world) as mcp:
        assert "demo_profile" not in {t.name for t in (await mcp.list_tools()).tools}
    hidden = await call(world, "demo_profile", {"lang": "en"})
    assert hidden.is_error and "Unknown tool" in text(hidden)
    assert [(site, t["name"]) for site, t in connector.registry.pending()] == [("demo", "profile")]
    assert connector.registry.share("demo", "profile") and not connector.registry.pending()
    connect(world)
    shared = await call(world, "demo_profile", {"lang": "en"})
    assert not shared.is_error, text(shared)


def test_the_extension_follows_its_own_recording_step_by_step(world):
    steps, release = [], threading.Event()

    def slow_model(shown):
        [job] = world["connector"].jobs.values()  # the id may not be there yet: the POST is still running
        steps.append(job.step)
        release.wait(5)
        return stub_model(shown)

    world["connector"].ask = slow_model
    started = httpx2.post(world["url"] + "/api/recordings", json=recording_of(world), headers=bearer(world))
    job_id = started.json()["job"]
    assert started.json()["state"] == "running"
    again = httpx2.post(world["url"] + "/api/recordings", json=recording_of(world), headers=bearer(world))
    assert again.status_code == 429  # one recording at a time per user
    for _ in range(200):
        if steps:
            break
        time.sleep(0.02)
    assert steps == ["Claude picks tools from 1 endpoints"]
    status = httpx2.get(f"{world['url']}/api/recordings/{job_id}", headers=bearer(world)).json()
    assert status == {"state": "running", "step": "Claude picks tools from 1 endpoints", "host": "127.0.0.1"}
    # Someone else does not see your recording.
    assert httpx2.get(f"{world['url']}/api/recordings/{job_id}", headers=bearer(world, "bob")).status_code == 404
    release.set()


def test_a_failed_extraction_says_why(world):
    nothing = {"host": "127.0.0.1", "log": {"entries": [{
        "request": {"method": "GET", "url": f"{world['site']}/app.js"},
        "response": {"status": 200, "content": {"mimeType": "text/javascript"}}}]}}
    job = extract(world, nothing)
    assert job["state"] == "failed" and "No API requests" in job["error"]
    assert httpx2.get(world["url"] + "/api/recordings/onbekend", headers=bearer(world)).status_code == 404


def test_someone_with_an_invite_makes_an_account_and_can_use_it_at_once(world):
    code = world["connector"].users.invite(uses=1)
    form = httpx2.get(f"{world['url']}/signup/{code}")
    assert form.status_code == 200 and "Create your account" in form.text
    taken = httpx2.post(f"{world['url']}/signup/{code}", headers=SAME_SITE,
                        data={"name": "anna", "password": "lang-genoeg-wachtwoord"})
    assert taken.status_code == 400 and "taken" in taken.text
    short = httpx2.post(f"{world['url']}/signup/{code}", headers=SAME_SITE,
                        data={"name": "carla", "password": "kort"})
    assert short.status_code == 400 and "at least 10 characters" in short.text
    welcome = httpx2.post(f"{world['url']}/signup/{code}", headers=SAME_SITE,
                          data={"name": "Carla", "password": "lang-genoeg-wachtwoord"})
    assert welcome.status_code == 200 and "Welcome, carla" in welcome.text
    assert welcome.headers["cache-control"] == "no-store" and f"{world['url']}/mcp" in welcome.text
    token = re.search(r'data-token="(usc_[\w-]+)"', welcome.text).group(1)
    # The page gives the token to the extension; only the script with this page's nonce may run.
    nonce = re.search(r"script-src 'nonce-([\w-]+)'", welcome.headers["content-security-policy"]).group(1)
    assert f'<script nonce="{nonce}">' in welcome.text and f'data-extension="{extension_id()}"' in welcome.text
    assert httpx2.get(world["url"] + "/api/sites", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert world["connector"].users.by_password("carla", "lang-genoeg-wachtwoord")["name"] == "carla"
    assert httpx2.get(f"{world['url']}/signup/{code}").status_code == 410  # this one worked once
    assert httpx2.get(f"{world['url']}/signup/verzonnen").status_code == 410


def test_a_sign_up_form_from_another_site_makes_no_account(world):
    # Otherwise a page elsewhere makes an account with its own invite, and the welcome page gives the visitor's
    # extension the token of that account: then the visitor links sessions into someone else's vault.
    code = world["connector"].users.invite(uses=2)
    other = httpx2.post(f"{world['url']}/signup/{code}", data={"name": "carla", "password": "lang-genoeg-wachtwoord"},
                        headers={"Sec-Fetch-Site": "cross-site"})
    assert "Make your account from this page" in other.text and "set-cookie" not in other.headers
    assert "carla" not in {u["name"] for u in world["connector"].users.all()}


def test_a_guest_at_the_limit_is_refused_before_the_recording_is_read(world):
    connector = world["connector"]
    object.__setattr__(connector.settings, "recordings_per_month", 0)
    guest = {"Authorization": f"Bearer {connector.users.redeem(connector.users.invite(), 'carla', 10)}"}
    refused = httpx2.post(world["url"] + "/api/recordings", content=b"{not json", headers=guest)
    assert refused.status_code == 429  # not 400: the body was never parsed


def test_only_the_team_adds_a_site_that_needs_the_browser(world):
    # A bot cookie in a recording makes a new site run in the server's Chrome: a guest could do that for any page.
    connector = world["connector"]
    guest = {"Authorization": f"Bearer {connector.users.redeem(connector.users.invite(), 'carla', 10)}"}
    protected = recording_of(world, "localhost")
    protected["log"]["entries"][0]["request"]["headers"] = [{"name": "Cookie", "value": "sid=s; _abck=x"}]
    refused = extract(world, protected, guest)
    assert refused["state"] == "failed" and "Only the team" in refused["error"]
    assert "localhost" not in connector.registry.ids()
    assert extract(world, protected)["state"] == "done"  # anna is on the team


def test_invited_users_have_a_monthly_limit_and_the_team_does_not(world):
    connector = world["connector"]
    object.__setattr__(connector.settings, "recordings_per_month", 2)
    guest = {"Authorization": f"Bearer {connector.users.redeem(connector.users.invite(), 'carla', 10)}"}
    nothing = {"host": "127.0.0.1", "log": {"entries": [{
        "request": {"method": "GET", "url": f"{world['site']}/app.js"},
        "response": {"status": 200, "content": {"mimeType": "text/javascript"}}}]}}
    for _ in range(3):  # without API requests: Claude is not called, so it does not count
        assert extract(world, nothing, guest)["state"] == "failed"
    assert extract(world, recording_of(world), guest)["state"] == "done"  # the server already knows demo
    assert extract(world, recording_of(world, "localhost"), guest)["result"]["new_site"] is True
    refused = httpx2.post(world["url"] + "/api/recordings", json=recording_of(world), headers=guest)
    assert refused.status_code == 429 and "2 times in the last 30 days" in refused.json()["error"]
    for _ in range(3):  # an admin made anna: no limit
        assert extract(world, recording_of(world))["state"] == "done"


def test_about_plays_the_video_from_the_data_folder(world):
    assert httpx2.get(world["url"] + "/about").status_code == 404  # no video yet
    (world["data"] / "about.mp4").write_bytes(b"0123456789")
    page = httpx2.get(world["url"] + "/about")
    assert page.status_code == 200 and '<video src="/about.mp4"' in page.text and "controls" not in page.text
    assert "media-src 'self'" in page.headers["content-security-policy"]
    part = httpx2.get(world["url"] + "/about.mp4", headers={"Range": "bytes=2-4"})  # seeking in the video
    assert part.status_code == 206 and part.content == b"234"


def test_the_extension_downloads_with_this_server_filled_in(world):
    response = httpx2.get(world["url"] + "/extension.zip")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as zipped:
        names = set(zipped.namelist())
        popup = zipped.read("mcp-everything-extension/popup.js").decode()
        manifest = json.loads(zipped.read("mcp-everything-extension/manifest.json"))
    assert {"mcp-everything-extension/manifest.json", "mcp-everything-extension/background.js"} <= names
    assert f'const DEFAULT_SERVER = "{world["url"]}";' in popup
    assert manifest["externally_connectable"] == {"matches": ["http://127.0.0.1/*"]}


def test_only_the_extension_may_call_the_api_from_a_browser(world):
    def preflight(origin):
        return httpx2.options(world["url"] + "/api/sites", headers={
            "Origin": origin, "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization"})

    ours = preflight(f"chrome-extension://{extension_id()}")
    assert ours.status_code == 200
    assert ours.headers["access-control-allow-origin"] == f"chrome-extension://{extension_id()}"
    assert preflight("https://kwaad.invalid").status_code == 400
    assert preflight("chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa").status_code == 400
    # The OAuth routes keep the SDK's CORS: a client in the browser is allowed there.
    token = httpx2.options(world["url"] + "/token", headers={"Origin": "https://claude.ai",
                                                             "Access-Control-Request-Method": "POST"})
    assert token.status_code == 200 and token.headers["access-control-allow-origin"] == "*"


def test_the_extension_links_a_session(world):
    linked = httpx2.post(world["url"] + "/api/sessions", headers=bearer(world, "bob"),
                        json={"url": world["site"] + "/pagina", "cookies": {"sid": "sessie-anna", "_abck": "bot"}})
    assert linked.json() == {"site": "demo"}
    sites = httpx2.get(world["url"] + "/api/sites", headers=bearer(world, "bob")).json()
    assert [s["connected"] for s in sites] == [True]
    unknown = httpx2.post(world["url"] + "/api/sessions", headers=bearer(world, "bob"),
                         json={"url": "https://onbekend.invalid", "cookies": {"a": "b"}})
    assert unknown.status_code == 422


def test_a_recording_is_validated_and_limited(world):
    assert httpx2.post(world["url"] + "/api/recordings", headers=bearer(world), content=b"{").status_code == 400
    assert httpx2.post(world["url"] + "/api/recordings", headers=bearer(world), json={"host": "x"}).status_code == 400
    chunks = (b" " * 1_000_000 for _ in range(30))  # without Content-Length: the server counts itself
    assert httpx2.post(world["url"] + "/api/recordings", headers=bearer(world), content=chunks).status_code == 413
