"""The browser runtime against a real headless Chrome. Without Chrome on this machine it is skipped."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from connector.browser import Browser
from connector.errors import ConnectorError
from connector.record import MAC_CHROME, chrome
from connector.runner import Runner
from connector.store import Store
from tests.helpers import PASSWORD, demo_manifest, demo_site, serve

HAS_CHROME = bool(os.environ.get("CHROME") or shutil.which("google-chrome") or Path(MAC_CHROME).exists())
pytestmark = pytest.mark.skipif(not HAS_CHROME, reason="no Chrome")


@pytest.fixture(scope="module")
def devtools(tmp_path_factory):
    with chrome(tmp_path_factory.mktemp("profiel"), headless=True) as (_, port, _path):
        yield f"http://127.0.0.1:{port}"


@pytest.fixture
def site():
    with serve(demo_site()) as url:
        yield url


def test_a_tool_runs_as_fetch_from_the_site_with_only_this_users_cookies(devtools, site):
    browser = Browser(devtools, site, allow_private=True)
    added = browser.request("POST", f"{site}/api/items", headers={"Cookie": "sid=sessie-anna"}, json={"name": "melk"})
    assert added.status_code == 200 and added.json() == {"item": {"id": "i1", "name": "melk"}}
    assert added.cookies == {"sid": "sessie-anna"}
    anonymous = browser.request("GET", f"{site}/api/items")
    assert anonymous.status_code == 401, "a new call must not see the cookies of the last one"


def test_cookies_the_site_sets_come_back_for_the_vault(devtools, site):
    browser = Browser(devtools, site, allow_private=True)
    login = browser.request("POST", f"{site}/login", json={"email": "anna@demo.invalid", "password": PASSWORD})
    assert login.status_code == 200 and login.cookies == {"sid": "sessie-anna"}
    assert json.loads(login.content) == {"ok": True}


def test_the_browser_only_goes_to_the_site_of_the_manifest(devtools, site):
    with pytest.raises(ConnectorError, match="outside the site"):
        Browser(devtools, site, allow_private=True).request("GET", "https://example.com/")
    with pytest.raises(ConnectorError, match="public addresses"):
        Browser(devtools, site).request("GET", f"{site}/api/items")


def test_the_runner_does_not_know_where_it_runs(devtools, site, tmp_path):
    runner = Runner({**demo_manifest(site), "runtime": "browser"}, Store("demo", tmp_path),
                    Browser(devtools, site, allow_private=True))
    assert runner.auth.connect({"email": "anna@demo.invalid", "password": PASSWORD})
    assert runner.write("add_item", {"name": "melk"}, "k1", confirmed=True) == {"item": {"id": "i1", "name": "melk"}}
    assert runner.call("list_items") == [{"id": "i1", "name": "melk"}]


def test_a_redirect_to_an_address_that_is_not_allowed_never_leaves_the_browser(devtools):
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
    from starlette.routing import Route

    hits = []

    def secret(request):
        hits.append(request.url.path)
        return JSONResponse({"geheim": True})

    routes = [Route("/", lambda r: HTMLResponse("<p>site</p>")), Route("/secret", secret)]
    with serve(Starlette(routes=routes)) as inside:
        redirect = [Route("/", lambda r: HTMLResponse("<p>site</p>")),
                    Route("/go", lambda r: RedirectResponse(f"{inside.replace('127.0.0.1', 'localhost')}/secret"))]
        with serve(Starlette(routes=redirect)) as site:
            browser = Browser(devtools, site, allow_private=True)
            browser.reachable = lambda url: "localhost" not in url  # localhost plays the internal address here
            with pytest.raises(Exception, match="fetch failed"):
                browser.request("GET", f"{site}/go")
    assert hits == []


def _hostile_site(inside: str, leaks: list):
    """A homepage with scripts that try to read the session, and that reach for `inside` from a frame and a worker."""
    import time as clock

    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Route

    page = f"""<script>
const x = new XMLHttpRequest(); x.open("GET", "/leak?cookie=" + encodeURIComponent(document.cookie), false); x.send();
const original = window.fetch;
window.fetch = (url, options) => {{
  const y = new XMLHttpRequest(); y.open("POST", "/leak", false); y.send(JSON.stringify(options)); return original(url, options);
}};
new Worker(URL.createObjectURL(new Blob(['fetch("{inside}/secret-worker")'])));
</script><iframe src="{inside}/secret-frame"></iframe>"""

    async def leak(request):
        leaks.append(request.query_params.get("cookie", "") + (await request.body()).decode())
        return JSONResponse({})

    def slow(request):
        clock.sleep(1)  # time for the frame and the worker
        return JSONResponse({"ok": True})

    return Starlette(routes=[Route("/", lambda r: HTMLResponse(page)), Route("/leak", leak, methods=["GET", "POST"]),
                             Route("/api/slow", slow)])


def test_scripts_of_the_page_never_see_the_session_and_frames_and_workers_are_checked(devtools):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    hits, leaks = [], []

    def secret(request):
        hits.append(request.url.path)
        return JSONResponse({})

    inner = Starlette(routes=[Route("/secret-frame", secret), Route("/secret-worker", secret)])
    with serve(inner) as inside, serve(_hostile_site(inside.replace("127.0.0.1", "localhost"), leaks)) as site:
        browser = Browser(devtools, site, allow_private=True)
        browser.reachable = lambda url: "localhost" not in url  # localhost plays the internal address here
        answer = browser.request("GET", f"{site}/api/slow", headers={"Cookie": "sid=geheim-sessie",
                                                                     "Authorization": "Bearer geheim-token"})
    assert answer.json() == {"ok": True}
    assert leaks == [""]  # the script ran, but saw no cookie, and its fetch wrapper saw nothing
    assert hits == []
