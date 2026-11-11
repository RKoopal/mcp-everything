"""The extension must summarize the same as connector/har.py and must not be allowed anything extra."""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from html import unescape
from pathlib import Path

import pytest

from connector import pages
from connector.har import summarize

EXTENSION = Path(__file__).parent.parent / "extension"
RUN = """
require(process.argv[1]);
let input = "";
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => {
  const { har, host } = JSON.parse(input);
  process.stdout.write(JSON.stringify(globalThis.connectorSummary.summarize(har, host)));
});
"""


def entry(url, method="GET", status=200, mime="application/json", body=None, post=None, query=(), page=None):
    text = body if isinstance(body, str) or body is None else json.dumps(body)
    return {"request": {"url": url, "method": method, "queryString": [{"name": q, "value": "SECRET"} for q in query],
                        "postData": {"text": post if isinstance(post, str) or post is None else json.dumps(post)}},
            "response": {"status": status, "content": {"mimeType": mime, **({"text": text} if text else {})}},
            "page": page}


PERSISTED = {"operationName": "AddItem", "variables": {"id": "SECRET", "n": 1},
             "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "abcdef0123456789abcdef"}}}
ENCODED = {"mimeType": "application/json", "encoding": "base64", "text": base64.b64encode(b'{"caf\xc3\xa9": 1}').decode()}

CASES = {
    "json and ids": [entry("https://a.nl/item/123;jsessionid=SECRET?x=1", body={"a": 1, "b": 1.5, "c": None, "d": True},
                           query=["x"]),
                     entry("https://a.nl/item/456", status=500, page="/p/{id}")],
    "nested lists": [entry("https://a.nl/s", body={"products": [[{"id": 1}]], "empty": [], "none": {}})],
    "dynamic keys": [entry("https://a.nl/d", body={"data": {"19748526": {"id": "1"}, "18702138": {"id": "2"}}})],
    "depth": [entry("https://a.nl/deep", body={"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}})],
    "persisted query": [entry("https://a.nl/graphql", "POST", post=PERSISTED, body={"data": {"id": "SECRET"}})],
    "only hash": [entry("https://a.nl/graphql", "POST", post={"extensions": PERSISTED["extensions"]})],
    "batch": [entry("https://a.nl/graphql", "POST", post=[{"operationName": "A"}, {"operationName": "B"}])],
    "operation in query": [{**entry("https://a.nl/graphql"),
                           "request": {"url": "https://a.nl/graphql", "method": "GET", "postData": {"text": None},
                                       "queryString": [{"name": "operationName", "value": "Lees"}]}}],
    "form": [entry("https://a.nl/login", "POST", post="user=SECRET&password=SECRET&leeg=&los")],
    "post with html": [entry("https://a.nl/graphql", "POST", mime="text/html", post={"operationName": "A"})],
    "pages": [entry("https://a.nl/page", mime="text/html"), entry("https://a.nl/page", mime="text/html"),
                 entry("https://a.nl/s/", mime="text/html", query=["searchtext"]),
                 entry("https://a.nl/app.js", mime="text/javascript")],
    "broken json": [entry("https://a.nl/x", body="{kapot")],
    "base64": [{**entry("https://a.nl/x"), "response": {"status": 200, "content": ENCODED}}],
}


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("host", ["", "a.nl", "ander.nl"])
def test_extension_summarizes_like_python(name, host):
    har = {"log": {"entries": CASES[name]}}
    done = subprocess.run(["node", "-e", RUN, str(EXTENSION / "summary.js")], input=json.dumps({"har": har, "host": host}),
                          capture_output=True, text=True, check=True)
    assert json.loads(done.stdout) == json.loads(json.dumps(summarize(har, host)))


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_extension_summary_holds_no_values():
    har = {"log": {"entries": [e for case in CASES.values() for e in case]}}
    done = subprocess.run(["node", "-e", RUN, str(EXTENSION / "summary.js")], input=json.dumps({"har": har, "host": ""}),
                          capture_output=True, text=True, check=True)
    assert "SECRET" not in done.stdout


RUN_MERGE = """
require(process.argv[1]);
const { emptySummary, merge, summarize } = globalThis.connectorSummary;
let input = "";
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => {
  const total = emptySummary();
  for (const entry of JSON.parse(input)) merge(total, summarize({ log: { entries: [entry] } }, ""));
  process.stdout.write(JSON.stringify(total));
});
"""
RUN_HELPERS = """
require(process.argv[1]);
const { bodyText, patternFor } = globalThis.connectorSummary;
const bytes = (text) => ({ bytes: new TextEncoder().encode(text).buffer });
process.stdout.write(JSON.stringify({
  patterns: ["www.bol.com", "bol.com", "a.b.nl", "127.0.0.1", "localhost"].map(patternFor),
  raw: bodyText({ raw: [bytes('{"a":'), bytes("1}"), { file: "x" }] }),
  form: bodyText({ formData: { user: ["a b"], tag: ["1", "2"] } }),
  empty: bodyText(undefined) ?? null,
}));
"""


def node(code, stdin=""):
    done = subprocess.run(["node", "-e", code, str(EXTENSION / "summary.js")], input=stdin,
                          capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_merging_one_request_at_a_time_equals_the_whole_summary():
    entries = [e for case in CASES.values() for e in case]
    assert node(RUN_MERGE, json.dumps(entries)) == json.loads(json.dumps(summarize({"log": {"entries": entries}})))


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_helpers_for_the_background_recording():
    result = node(RUN_HELPERS)
    assert result["patterns"] == ["*://*.bol.com/*", "*://*.bol.com/*", "*://*.a.b.nl/*", "*://127.0.0.1/*", "*://localhost/*"]
    assert result["raw"] == '{"a":1}'
    assert result["form"] == "user=a+b&tag=1&tag=2"
    assert result["empty"] is None


RUN_MASK = """
require(process.argv[1]);
const { maskPassword } = globalThis.connectorSummary;
const texts = ['{"username":"u","password":"SECRET","isPassCodeReset":false,"a":[{"newPassword":"SECRET"}]}',
               "user=u&password=SECRET", "kale tekst", ""];
process.stdout.write(JSON.stringify(texts.map(maskPassword)));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_passwords_are_masked_but_other_values_stay():
    masked = node(RUN_MASK)
    assert json.loads(masked[0]) == {"username": "u", "password": "***", "isPassCodeReset": False,
                                     "a": [{"newPassword": "***"}]}
    assert masked[1] == "user=u&password=***"
    assert masked[2:] == ["kale tekst", ""]


def test_extension_gets_no_site_access_until_the_user_grants_it():
    manifest = json.loads((EXTENSION / "manifest.json").read_text())
    assert "host_permissions" not in manifest and "content_scripts" not in manifest
    assert manifest["optional_host_permissions"] == ["<all_urls>"]
    # Only the server's sign-up page may send the extension something: the token after signing up.
    assert manifest["externally_connectable"] == {"matches": ["https://mcp-everything.com/*"]}
    # cookies reads only sites the user gave access to, on "Link session". Everything goes to the server,
    # so no downloads.
    # scripting: catch.js on a site's login page, only after the user gave access to that site.
    assert set(manifest["permissions"]) == {"webRequest", "storage", "activeTab", "cookies", "scripting"}


# The background script with a faked chrome and server. setTimeout does not wait, so following goes fast.
BACKGROUND = r"""
const path = require("path");
const dir = path.dirname(process.argv[1]);
const listeners = {};
const on = (name) => ({ addListener: (fn) => (listeners[name] ??= []).push(fn) });
const copy = (value) => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const area = (data) => ({
  data,
  get: async (key) => ({ [key]: copy(data[key]) }),
  set: async (items) => { Object.assign(data, copy(items)); },
  remove: async (key) => { delete data[key]; },
});
const granted = new Set();
const log = [];
const extractions = [];
const session = area({});
const sessionSet = session.set;
session.set = async (items) => { if (items.extraction) extractions.push(copy(items.extraction)); return sessionSet(items); };
const answers = [];
const posted = [];
const external = on("external");
globalThis.chrome = {
  storage: { session, local: area({ server: { url: "https://server.test", token: "usc_test" } }) },
  action: { setBadgeText: async ({ text }) => { log.push(["badge", text]); }, openPopup: async () => { log.push(["popup"]); } },
  tabs: { reload: async (id) => { log.push(["reload", id]); }, create: async (tab) => { log.push(["open", tab]); },
          remove: async (id) => { log.push(["close", id]); }, update: async (id, change) => { log.push(["tab", id, change]); } },
  permissions: { contains: async ({ origins }) => origins.every((o) => granted.has(o)), onAdded: on("permissions") },
  webRequest: { onBeforeRequest: on("request"), onBeforeSendHeaders: on("headers"), onErrorOccurred: on("error"),
                onCompleted: on("completed"), onBeforeRedirect: on("redirect") },
  runtime: { onMessage: on("message"), onMessageExternal: external, getURL: (file) => `chrome-extension://abc/${file}` },
  cookies: { getAll: async () => [] },
  scripting: { unregisterContentScripts: async ({ ids }) => { log.push(["unregister", ids]); } },
};
globalThis.importScripts = (file) => require(path.join(dir, file));
globalThis.setTimeout = (fn) => setImmediate(fn);
globalThis.fetch = async (url, options = {}) => {
  posted.push({ url, method: options.method ?? "GET", auth: options.headers.Authorization,
                body: options.body && JSON.parse(options.body) });
  const answer = answers.shift();
  if (answer instanceof Error) throw answer;
  return { ok: answer.status < 400, status: answer.status, json: async () => answer.body };
};
require(path.join(dir, "background.js"));

const send = (message) => new Promise((resolve) => listeners.message[0](message, {}, resolve));
const settle = () => new Promise((resolve) => setImmediate(resolve));
const until = async (check) => {
  for (let i = 0; i < 1000; i += 1) {
    if (await check()) return;
    await settle();
  }
  throw new Error("did not happen");
};
const allow = (origins) => {
  origins.forEach((origin) => granted.add(origin));
  listeners.permissions.forEach((fn) => fn({ origins }));
};
const visit = (url) => {
  const details = { requestId: url, tabId: 7, url, method: "GET", statusCode: 200, type: "xmlhttprequest",
                    requestHeaders: [{ name: "Cookie", value: "sid=1" }],
                    responseHeaders: [{ name: "Content-Type", value: "application/json" }] };
  listeners.request.forEach((fn) => fn(details));
  listeners.headers.forEach((fn) => fn(details));
  return Promise.all(listeners.completed.map((fn) => fn(details)));
};
const out = (value) => process.stdout.write(JSON.stringify(value));
const ORIGINS = ["*://*.a.nl/*"];
const START = { type: "start", tabId: 7, url: "https://a.nl/", host: "a.nl" };
"""


def background(scenario):
    done = subprocess.run(["node", "-e", BACKGROUND + f"(async () => {{ {scenario} }})();", str(EXTENSION / "summary.js")],
                          capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_the_recording_starts_once_permission_is_given_even_after_the_popup_closed():
    # Chrome closes the popup as soon as it asks for permission. Only the background script still hears the yes.
    result = background("""
        const asked = await send({ type: "arm", origins: ORIGINS, next: START });
        const before = await send({ type: "status" });
        allow(ORIGINS);
        await until(() => send({ type: "status" }));
        allow(ORIGINS);  // once more: no second recording
        await settle();
        out({ asked, before, after: await send({ type: "status" }), log });
    """)
    assert result["asked"] is None and result["before"] is None
    assert result["after"]["host"] == "a.nl"
    assert result["log"] == [["badge", "REC"], ["reload", 7]]


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_with_permission_already_given_the_popup_gets_the_answer_at_once():
    result = background("""
        granted.add(ORIGINS[0]);  // Chrome asks nothing, and reports nothing new either
        const asked = await send({ type: "arm", origins: ORIGINS, next: START });
        const reloads = log.length;
        // If the popup and a new permission come at the same time, still only one recording starts.
        await Promise.all([send({ type: "arm", origins: ORIGINS, next: START }), allow(ORIGINS)]);
        await settle();
        out({ asked, first: reloads, log });
    """)
    assert result["asked"] == {"ok": True} and result["first"] == 2
    assert result["log"] == [["badge", "REC"], ["reload", 7]] * 2


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_only_starting_and_linking_wait_for_permission():
    result = background("""
        granted.add(ORIGINS[0]);
        out(await send({ type: "arm", origins: ORIGINS, next: { type: "disconnect" } }));
    """)
    assert result is None


def test_the_extension_has_a_fixed_id_that_the_server_knows():
    # The key in manifest.json sets the id, also for an unpacked extension. The server allows that id.
    from connector.server import extension_id
    assert extension_id() == "mbgeknjocembkhfbaaaapcjphmojkjdn"


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_the_signup_page_hands_over_the_token_and_nothing_else():
    result = background("""
        chrome.storage.local.data.server = undefined;
        const tell = (message) => new Promise((resolve) => {
          if (!listeners.external[0](message, { origin: "https://connector.test", url: "https://connector.test/x" },
                                     resolve)) resolve("not handled");
        });
        const before = await tell({ type: "ping" });
        const wrong = await tell({ type: "setup", token: "geen-token" });
        answers.push({ status: 200, body: [{ site: "demo" }] });
        const setup = await tell({ type: "setup", token: "usc_nieuw" });
        const after = await tell({ type: "ping" });
        out({ before, wrong, setup, after, server: chrome.storage.local.data.server, posted, popup: log.at(-1) });
    """)
    assert result["before"] == {"ok": True, "connected": False} and result["wrong"] == "not handled"
    assert result["setup"] == {"ok": True, "sites": 1}
    assert result["server"] == {"url": "https://connector.test", "token": "usc_nieuw"}
    assert result["posted"] == [{"url": "https://connector.test/api/sites", "method": "GET", "auth": "Bearer usc_nieuw"}]
    assert result["after"] == {"ok": True, "connected": True} and result["popup"] == ["popup"]


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_the_login_page_gets_the_token_only_from_the_server_it_belongs_to():
    result = background("""
        chrome.storage.local.data.server = { url: "https://connector.test", token: "usc_van_anna" };
        const ask = (origin) => new Promise((resolve) => {
          listeners.external[0]({ type: "token" }, { origin, url: `${origin}/login/x` }, resolve);
        });
        const own = await ask("https://connector.test");
        const other = await ask("https://andere-server.test");
        chrome.storage.local.data.server = undefined;
        const none = await ask("https://connector.test");
        out({ own, other, none });
    """)
    assert result["own"] == {"ok": True, "token": "usc_van_anna"}
    assert result["other"] == {"ok": False} and result["none"] == {"ok": False}


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_after_stopping_the_extension_follows_the_server_until_the_tools_are_made():
    result = background("""
        granted.add(ORIGINS[0]);
        await send({ type: "arm", origins: ORIGINS, next: START });
        await visit("https://a.nl/api/items?q=fiets");
        answers.push(
          { status: 202, body: { job: "j1", state: "running", step: "Recording received", host: "a.nl" } },
          { status: 200, body: { state: "running", step: "Claude picks tools from 1 endpoints", host: "a.nl" } },
          new TypeError("Failed to fetch"),  // no network once: just keep asking
          { status: 200, body: { state: "done", step: "Checking and saving tools", host: "a.nl",
                                 result: { site: "a", added: ["a_items"], rejected: [], session: true } } });
        const stopped = await send({ type: "stop" });
        await until(() => extractions.at(-1)?.state === "done");
        out({ stopped, posted, steps: extractions.map((e) => `${e.state}: ${e.step}`), last: extractions.at(-1),
              badge: log.filter(([kind]) => kind === "badge").map(([, text]) => text) });
    """)
    assert result["stopped"] == {"count": 1}
    post, *polls = result["posted"]
    assert post["url"] == "https://server.test/api/recordings" and post["method"] == "POST"
    assert post["auth"] == "Bearer usc_test"
    assert [e["request"]["url"] for e in post["body"]["log"]["entries"]] == ["https://a.nl/api/items?q=fiets"]
    assert {p["url"] for p in polls} == {"https://server.test/api/recordings/j1"}
    assert result["steps"] == ["running: Sending recording", "running: Recording received",
                               "running: Claude picks tools from 1 endpoints", "running: Claude picks tools from 1 endpoints",
                               "done: Checking and saving tools"]
    assert result["last"]["result"]["added"] == ["a_items"]
    assert result["badge"] == ["REC", "…", "✓"]


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_an_answer_from_the_server_ends_following_at_once():
    result = background("""
        await send({ type: "extract", host: "a.nl", entries: [] });
        await until(() => extractions.at(-1)?.state === "failed");
        out({ last: extractions.at(-1), badge: log.at(-1) });
    """.replace("await send", 'answers.push({ status: 202, body: { job: "j1", state: "running", step: "x" } }, '
                               '{ status: 404, body: { error: "The server does not know this recording." } });\n await send', 1))
    assert result["last"]["error"] == "The server does not know this recording."
    assert result["badge"] == ["badge", "!"]


# A page of the server next to the extension above. The page's chrome.runtime only appears once the extension is
# "installed" (after `install` waits; 0: before the page loads), as in Chrome. Each message goes to the extension's
# onMessageExternal.
PAGE = r"""
chrome.storage.local.data.server = undefined;
const { script, data, install, fails } = INPUT;
const classes = new Set();
const failed = { hidden: true, textContent: "" };
const waits = [];
const page = {};
const document = {
  querySelector: () => ({ dataset: data }),
  querySelectorAll: () => [],
  getElementById: (id) => (id === "failed" ? failed : null),
  body: { classList: { toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)) } },
};
const sender = { origin: "https://connector.test", url: "https://connector.test/login/x" };
const installed = () => (page.runtime = { sendMessage: (id, message, callback) => {
  if (!listeners.external[0](message, sender, callback)) callback(undefined);
} });
if (install === 0) installed();
const wait = (fn, ms) => {
  waits.push({ ms, token: chrome.storage.local.data.server?.token ?? null });
  if (waits.length === install) installed();
  setImmediate(fn);
};
answers.push(fails ? { status: 500, body: { error: "Down for a moment." } } : { status: 200, body: [{ site: "demo" }] });
new Function("window", "document", "chrome", "setTimeout", script)({ chrome: page }, document, page, wait);
await until(() => classes.has("found") || !failed.hidden);
out({ waits, found: classes.has("found"), failed: failed.hidden ? null : failed.textContent,
      server: chrome.storage.local.data.server ?? null, posted });
"""


def page_with_extension(response, install=3, fails=False) -> dict:
    """Runs the script of a rendered page, with its data attributes, next to the extension."""
    html = response.body.decode()
    script = re.search(r'<script nonce="[^"]+">(.*)</script>', html, re.S).group(1)
    data = {k: unescape(v) for k, v in re.findall(r' data-(\w+)="([^"]*)"', re.search(r"<main[^>]*>", html).group(0))}
    data["extension"] = "abc"  # the extension does not check its own id
    inputs = json.dumps({"script": script, "data": data, "install": install, "fails": fails})
    return background(PAGE.replace("INPUT", inputs))


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_the_welcome_step_gives_the_extension_the_token_once_it_is_installed():
    # Made the account first, installed the extension after: the page asks every 2 s, then hands over the token.
    result = page_with_extension(pages.login_welcome("Claude", "claude.ai", "carla", "usc_nieuw", "abc", "'self'"))
    assert result["waits"] == [{"ms": 2000, "token": None}] * 3  # the extension had no token while it was missing
    assert result["found"] and result["failed"] is None
    assert result["server"] == {"url": "https://connector.test", "token": "usc_nieuw"}
    assert result["posted"] == [{"url": "https://connector.test/api/sites", "method": "GET", "auth": "Bearer usc_nieuw"}]
    # Installed first: the extension answers at once, and gets the token right away. /signup's welcome page as well.
    welcome = page_with_extension(pages.welcome("carla", "usc_nieuw", "https://connector.test", "abc"), install=0)
    assert welcome["waits"] == [] and welcome["found"] and welcome["server"]["token"] == "usc_nieuw"


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_before_the_account_exists_the_page_only_asks_if_the_extension_is_there():
    result = page_with_extension(pages.signup("abc"), install=2)
    assert len(result["waits"]) == 2 and result["found"]  # ✓ without a reload
    assert result["server"] is None and result["posted"] == []


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_the_welcome_step_says_so_when_the_extension_could_not_connect():
    result = page_with_extension(pages.login_welcome("Claude", "claude.ai", "carla", "usc_nieuw", "abc", "'self'"),
                                 install=0, fails=True)
    assert not result["found"] and result["failed"] == "Down for a moment."
    assert result["server"] is None  # the extension does not keep a token that did not work


AUTHORIZE = "https://login.ah.nl/secure/oauth/authorize?client_id=appie&redirect_uri=appie://login-exit"


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_the_extension_catches_a_login_code_only_for_the_link_page_that_asked():
    # AH sends you to appie://login-exit?code=… after the login. Chrome cannot open that, but webRequest sees it.
    result = background("""
        const ask = (message, tab = 3, origin = "https://connector.test") => new Promise((resolve) => {
          listeners.external[0](message, { origin, url: `${origin}/connect/x`, tab: { id: tab } }, resolve);
        });
        const redirect = (redirectUrl) => Promise.all(listeners.redirect.map((fn) => fn({ tabId: 9, redirectUrl })));
        const web = await ask({ type: "oauth", authorize: AUTHORIZE, redirect: "https://elders.test/" });
        const watching = await ask({ type: "oauth", authorize: AUTHORIZE, redirect: "appie://login-exit" });
        const early = await ask({ type: "code" });
        await redirect("https://login.ah.nl/secure/next");
        await redirect("appie://login-exit?code=abc");
        const otherTab = await ask({ type: "code" }, 4);
        const otherServer = await ask({ type: "code" }, 3, "https://andere-server.test");
        const code = await ask({ type: "code" });
        const again = await ask({ type: "code" });
        out({ web, watching, early, otherTab, otherServer, code, again, log });
    """.replace("AUTHORIZE", json.dumps(AUTHORIZE)))
    assert result["web"] == {"ok": False}  # a normal address opens the site's own page: nothing to catch
    assert result["watching"] == {"ok": True} and result["early"] == {"ok": True, "address": None}
    assert result["otherTab"] == {"ok": False} and result["otherServer"] == {"ok": False}
    assert result["code"] == {"ok": True, "address": "appie://login-exit?code=abc"}
    assert result["again"] == {"ok": False}  # only once
    # The login opens next to the link page; after the redirect its tab closes and the link page is in front again.
    assert result["log"] == [["open", {"url": "chrome-extension://abc/login.html", "openerTabId": 3}],
                             ["unregister", ["login"]], ["close", 9], ["tab", 3, {"active": True}]]


# The link page next to the extension: a click, the login in another tab, and the form goes with the address.
LINK_PAGE = r"""
const { script, data } = INPUT;
const classes = new Set();
const element = () => ({ hidden: true, open: true, textContent: "", listeners: {},
                         addEventListener(type, fn) { this.listeners[type] = fn; } });
const submitted = [];
const form = { elements: { code: { value: "" } }, requestSubmit: () => submitted.push(form.elements.code.value) };
const elements = { paste: element(), waiting: element(), failed: element(), extension: element(), link: form };
const document = {
  querySelector: () => ({ dataset: data }),
  getElementById: (id) => elements[id],
  body: { classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name) } },
};
const sender = { origin: "https://connector.test", url: "https://connector.test/connect/x", tab: { id: 3 } };
const page = { runtime: { sendMessage: (id, message, callback) => {
  if (!listeners.external[0](message, sender, callback)) callback(undefined);
} } };
const later = (fn) => setImmediate(fn);
new Function("window", "document", "chrome", "setTimeout", script)({ chrome: page }, document, page, later);
await until(() => classes.has("found"));
const closed = !elements.paste.open;
await elements.extension.listeners.click();
const waiting = !elements.waiting.hidden;
await settle();
await Promise.all(listeners.redirect.map((fn) => fn({ tabId: 9, redirectUrl: "appie://login-exit?code=abc" })));
await until(() => submitted.length);
out({ closed, waiting, submitted, opened: log[0], failed: elements.failed.hidden ? null : elements.failed.textContent });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_with_the_extension_the_link_page_needs_no_pasting():
    response = pages.oauth("ah", AUTHORIZE, "appie://login-exit", "abc")
    html = response.body.decode()
    script = re.search(r'<script nonce="[^"]+">(.*)</script>', html, re.S).group(1)
    data = {k: unescape(v) for k, v in re.findall(r' data-(\w+)="([^"]*)"', re.search(r"<main[^>]*>", html).group(0))}
    assert data == {"extension": "abc", "authorize": AUTHORIZE, "redirect": "appie://login-exit"}
    result = background(LINK_PAGE.replace("INPUT", json.dumps({"script": script, "data": data})))
    assert result["closed"] and result["waiting"] and result["failed"] is None  # pasting folds away
    assert result["opened"] == ["open", {"url": "chrome-extension://abc/login.html", "openerTabId": 3}]
    assert result["submitted"] == ["appie://login-exit?code=abc"]  # the server reads the code from the address


def test_without_the_extension_the_link_page_says_to_turn_it_on_and_you_can_paste():
    html = pages.oauth("ah", AUTHORIZE, "appie://login-exit").body.decode()
    assert 'class="missing-only"><p>Turn on the MCP Everything extension' in html
    assert '<details id="paste" open>' in html and '<form id="link" method="post">' in html
    assert 'href="/extension.zip"' in html


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_the_login_page_script_reports_a_jump_to_the_apps_address():
    # AH's page goes to appie://login-exit?code=… with JavaScript: no request, so only the navigate event shows it.
    done = subprocess.run(["node", "-e", """
        const handlers = [];
        const sent = [];
        globalThis.navigation = { addEventListener: (type, fn) => handlers.push([type, fn]) };
        globalThis.chrome = { runtime: { sendMessage: (message) => sent.push(message) } };
        require(process.argv[1]);
        for (const url of ["https://login.ah.nl/login/mfa", "about:blank", "appie://login-exit?code=abc"]) {
          handlers.forEach(([, fn]) => fn({ destination: { url } }));
        }
        process.stdout.write(JSON.stringify({ types: handlers.map(([type]) => type), sent }));
    """, str(EXTENSION / "catch.js")], capture_output=True, text=True, check=True)
    result = json.loads(done.stdout)
    assert result["types"] == ["navigate"]
    assert result["sent"] == [{"type": "caught", "url": "appie://login-exit?code=abc"}]


@pytest.mark.skipif(not shutil.which("node"), reason="node not found")
def test_the_extension_takes_the_address_from_its_login_page_script():
    result = background("""
        const ask = (message) => new Promise((resolve) => {
          listeners.external[0](message, { origin: "https://connector.test", url: "https://connector.test/connect/x",
                                           tab: { id: 3 } }, resolve);
        });
        const report = (url) => new Promise((resolve) => listeners.message[0]({ type: "caught", url }, { tab: { id: 9 } },
                                                                               resolve));
        const before = await report("appie://login-exit?code=vroeg");  // nobody waits yet
        await ask({ type: "oauth", authorize: AUTHORIZE, redirect: "appie://login-exit" });
        const other = await report("mailto:klantenservice@ah.nl");
        const caught = await report("appie://login-exit?code=abc");
        const code = await ask({ type: "code" });
        out({ before, other, caught, code, log: log.slice(1) });
    """.replace("AUTHORIZE", json.dumps(AUTHORIZE)))
    assert result["before"] == {"ok": False} and result["other"] == {"ok": False}
    assert result["caught"] == {"ok": True}
    assert result["code"] == {"ok": True, "address": "appie://login-exit?code=abc"}
    assert result["log"] == [["unregister", ["login"]], ["close", 9], ["tab", 3, {"active": True}]]
