// Background recording. Reads only sites the user gave permission for, and only while
// a recording runs. Every request is summarized at once. Passwords are masked.
// The recording goes to the server, which makes tools from it. That takes a while: this script follows the
// progress and puts it in chrome.storage.session, where the popup reads it.
// webRequest shows no responses, so for those you need the DevTools recording.
"use strict";

importScripts("summary.js");

const { bodyText, hostOf, maskPassword, summarize, template } = globalThis.connectorSummary;

const MAX_TICKS = 44;
const MAX_RECENT = 4;
const MAX_VALUES = 300;
const POLL = 2000;
const MISSES = 3;
const ARMED_FOR = 120_000;

const bodies = new Map();
const headers = new Map();
let state = null;
const ready = chrome.storage.session.get("recording").then(({ recording }) => {
  state = recording ?? null;
});

const save = () => chrome.storage.session.set({ recording: state });
const watched = (details) => details.tabId === state.tabId || details.tabId === -1;

function mimeType(details) {
  const header = (details.responseHeaders || []).find((h) => h.name.toLowerCase() === "content-type");
  return header?.value ?? "";
}

chrome.webRequest.onBeforeRequest.addListener((details) => {
  bodies.set(details.requestId, bodyText(details.requestBody));
}, { urls: ["<all_urls>"] }, ["requestBody"]);

chrome.webRequest.onBeforeSendHeaders.addListener((details) => {
  headers.set(details.requestId, details.requestHeaders);
}, { urls: ["<all_urls>"] }, ["requestHeaders", "extraHeaders"]);

chrome.webRequest.onErrorOccurred.addListener((details) => {
  bodies.delete(details.requestId);
  headers.delete(details.requestId);
}, { urls: ["<all_urls>"] });

chrome.webRequest.onCompleted.addListener(async (details) => {
  const text = bodies.get(details.requestId);
  const sent = headers.get(details.requestId) ?? [];
  bodies.delete(details.requestId);
  headers.delete(details.requestId);
  await ready;
  if (!state || !watched(details)) return;
  const url = new URL(details.url);
  if (details.type === "main_frame") state.page = template(url.pathname);
  const entry = {
    request: {
      url: details.url,
      method: details.method,
      queryString: [...url.searchParams].map(([name, value]) => ({ name, value })),
      postData: { text },
    },
    response: { status: details.statusCode, content: { mimeType: mimeType(details) } },
    page: state.page,
  };
  const part = summarize({ log: { entries: [entry] } }, state.host);
  const [action] = part.api;
  const kind = action ? "action" : Object.keys(part.documents).length ? "page" : part.static ? "file" : null;
  if (!kind) return;
  state.count += 1;
  state.ticks = [...state.ticks, kind].slice(-MAX_TICKS);
  if (action) {
    const name = action.operation || `${action.method} ${action.path}`;
    state.recent = [name, ...state.recent.filter((other) => other !== name)].slice(0, MAX_RECENT);
    const value = { method: details.method, url: details.url, status: details.statusCode, mime: mimeType(details),
                    headers: Object.fromEntries(sent.map((h) => [h.name, h.value])), body: maskPassword(text) };
    state.values = [...state.values, value].slice(-MAX_VALUES);
  }
  await save();
}, { urls: ["<all_urls>"] }, ["responseHeaders"]);

async function start({ tabId, url, host }) {
  await ready;
  state = { tabId, url, host, page: null, count: 0, ticks: [], recent: [], values: [] };
  await save();
  await chrome.action.setBadgeText({ text: "REC" });
  await chrome.tabs.reload(tabId);  // so the recording runs from the start of the page
  return { ok: true };
}

async function stop() {
  await ready;
  if (!state) return null;
  const { host, count, values } = state;
  state = null;
  await save();
  await extract({ host, entries: values.map(asEntry) });
  return { count };
}

async function status() {
  await ready;
  return state && { host: state.host, count: state.count, ticks: state.ticks, recent: state.recent };
}

// ---------- permission ----------
// When the popup asks for permission, Chrome often closes the popup, and the code there stops.
// So the popup first says what must happen (arm). This script does it once permission is given.

const ARMABLE = new Set(["start", "link"]);
let firing = Promise.resolve();

async function arm({ origins, next }) {
  if (!ARMABLE.has(next.type)) return null;
  await chrome.storage.session.set({ armed: { origins, next, until: Date.now() + ARMED_FOR } });
  return fire();
}

function fire() {
  // One after the other, so the permission and the popup together do not start two recordings.
  const run = firing.then(async () => {
    const { armed } = await chrome.storage.session.get("armed");
    if (!armed || !await chrome.permissions.contains({ origins: armed.origins })) return null;
    await chrome.storage.session.remove("armed");
    return armed.until < Date.now() ? null : actions[armed.next.type](armed.next);
  });
  firing = run.catch(() => null);
  return run;
}

chrome.permissions.onAdded.addListener(() => {
  fire();
});

// An outcome the user must see, also when Chrome closed the popup.
async function tell(text, ok) {
  await chrome.storage.session.set({ notice: { text, ok } });
  await chrome.action.openPopup?.().catch(() => {});  // does not always work; then it shows on the next click
}

// ---------- the server ----------

const serverSettings = async () => (await chrome.storage.local.get("server")).server ?? null;

async function api(path, options = {}) {
  const server = await serverSettings();
  if (!server) throw new Error("No server set up. Click Set up at the bottom of the popup.");
  const response = await fetch(`${server.url}${path}`, {
    ...options,
    headers: { Authorization: `Bearer ${server.token}`, "Content-Type": "application/json" },
  });
  const answer = await response.json().catch(() => ({}));
  if (response.status === 401) throw Object.assign(new Error("The server does not know this token."), { status: 401 });
  if (!response.ok) {
    throw Object.assign(new Error(answer.error ?? `The server answered HTTP ${response.status}.`), { status: response.status });
  }
  return answer;
}

const attempt = (work) => work.then((answer) => ({ ok: true, ...answer }),
                                    (error) => ({ ok: false, error: error.message, status: error.status }));

function asEntry(value) {
  return {
    request: { method: value.method, url: value.url, postData: { text: value.body },
               headers: Object.entries(value.headers).map(([name, header]) => ({ name, value: header })) },
    response: { status: value.status, content: { mimeType: value.mime ?? "" } },
  };
}

async function configure({ url, token }) {
  await chrome.storage.local.set({ server: { url: url.replace(/\/+$/, ""), token } });
  const result = await attempt(api("/api/sites").then((sites) => ({ sites: sites.length })));
  if (!result.ok) await chrome.storage.local.remove("server");
  await tell(result.ok ? `Connected to ${new URL(url).host}. Open the site you want to record.`
                       : `Could not connect: ${result.error}`, result.ok);
  return result;
}

async function lookup({ host }) {
  if (!await serverSettings()) return { ok: false, server: false };
  return attempt(api("/api/sites").then((sites) => ({
    server: true, site: sites.find((site) => hostOf(new URL(site.base).hostname) === host) ?? null,
  })));
}

// ---------- making tools ----------
// Chrome can cut off a fetch that waits longer than 30 seconds. So the server answers at once with an
// id, and this script asks every few seconds how far it got.

const setExtraction = (extraction) => chrome.storage.session.set({ extraction });
let following = null;

async function extract({ host, entries }) {
  const extraction = { host, state: "running", step: "Sending recording", started: Date.now() };
  await setExtraction(extraction);
  await chrome.action.setBadgeText({ text: "…" });
  const sent = await attempt(api("/api/recordings", { method: "POST", body: JSON.stringify({ host, log: { entries } }) }));
  if (!sent.ok) return finish({ ...extraction, state: "failed", error: sent.error });
  follow({ ...extraction, job: sent.job, step: sent.step });
  return { ok: true };
}

async function follow(extraction) {
  following = extraction.job;
  let misses = 0;
  while (extraction.state === "running") {
    await setExtraction(extraction);  // also a sign of life: that keeps Chrome running this script
    await new Promise((resolve) => setTimeout(resolve, POLL));
    const job = await attempt(api(`/api/recordings/${extraction.job}`));
    if (job.ok) {
      misses = 0;
      extraction = { ...extraction, state: job.state, step: job.step, result: job.result, error: job.error };
    } else if (job.status || ++misses >= MISSES) {  // an answer from the server counts at once, a network error only after three times
      extraction = { ...extraction, state: "failed", error: job.error };
    }
  }
  following = null;
  return finish(extraction);
}

async function finish(extraction) {
  await setExtraction(extraction);
  await chrome.action.setBadgeText({ text: extraction.state === "done" ? "✓" : "!" });
  return extraction;
}

async function extracted() {
  const { extraction } = await chrome.storage.session.get("extraction");
  if (extraction?.state === "running") {
    if (extraction.job && following !== extraction.job) follow(extraction);  // Chrome stopped this script in between
  } else {
    await chrome.action.setBadgeText({ text: state ? "REC" : "" });
  }
  return extraction ?? null;
}

async function link({ url }) {
  const cookies = await chrome.cookies.getAll({ url });
  const session = Object.fromEntries(cookies.map((cookie) => [cookie.name, cookie.value]));
  const result = await attempt(api("/api/sessions", { method: "POST", body: JSON.stringify({ url, cookies: session }) }));
  await tell(result.ok ? `Linked your ${hostOf(new URL(url).hostname)} session to your server.` : result.error, result.ok);
  return result;
}

// ---------- linking with a code ----------
// Some sites give their login code only to their own app: after you log in, they send you to an app address (the
// redirect_uri in the site's manifest, for example appie://login-exit?code=…) that Chrome cannot open. The server's
// link page asks this script to watch for it (watchLogin). login.html asks for access to the site, puts catch.js on
// it and opens its login. A jump there with JavaScript shows in catch.js, a real redirect in webRequest. Only the
// link page that asked gets the address (loginAddress), and it sends that to the server itself.

const LOGIN_FOR = 600_000;  // as long as a link from the server works

async function watchLogin({ authorize, redirect }, sender) {
  const custom = /^[a-z][a-z\d+.-]*:\/\//i.test(redirect) && !/^https?:/i.test(redirect);
  if (!sender.tab || typeof authorize !== "string" || !authorize.startsWith("https://") || !custom) return { ok: false };
  const login = { tab: sender.tab.id, origin: sender.origin, authorize, redirect, until: Date.now() + LOGIN_FOR,
                  address: null };
  await chrome.storage.session.set({ login });
  await chrome.tabs.create({ url: chrome.runtime.getURL("login.html"), openerTabId: sender.tab.id });
  return { ok: true };
}

async function loginAddress(_message, sender) {
  const { login } = await chrome.storage.session.get("login");
  if (!login || login.tab !== sender.tab?.id || login.origin !== sender.origin || login.until < Date.now()) {
    return { ok: false };
  }
  if (login.address) await chrome.storage.session.remove("login");
  return { ok: true, address: login.address };
}

async function catchAddress(url, tabId) {
  const { login } = await chrome.storage.session.get("login");
  if (!login || login.address || login.until < Date.now() || !url.startsWith(login.redirect)) return { ok: false };
  await chrome.storage.session.set({ login: { ...login, address: url } });
  await chrome.scripting.unregisterContentScripts({ ids: ["login"] }).catch(() => {});
  await chrome.tabs.remove(tabId).catch(() => {});  // it only shows a spinner; the link page goes on
  await chrome.tabs.update(login.tab, { active: true }).catch(() => {});
  return { ok: true };
}

chrome.webRequest.onBeforeRedirect.addListener((details) => {
  catchAddress(details.redirectUrl, details.tabId);
}, { urls: ["<all_urls>"] });

const caught = ({ url }, sender) => catchAddress(String(url), sender.tab?.id);

const disconnect = () => chrome.storage.local.remove("server").then(() => ({ ok: true }));
const server = () => serverSettings().then((settings) => settings && { url: settings.url });
// Without a token: Claude Code logs in to the server itself (OAuth), with this extension's button on the login page.
const claude = () => serverSettings().then((settings) => settings &&
  `claude mcp add --transport http mcp-everything ${settings.url}/mcp`);

const actions = { start, stop, status, arm, configure, disconnect, server, lookup, extract, link, extracted, claude,
                  caught };

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  actions[message.type](message, sender).then(reply);
  return true;
});

// The server's sign-up page passes the token on itself, so nobody has to copy it. Only pages
// from externally_connectable in manifest.json can send this, and the server is that page's origin.
// The login page (OAuth, for Claude) asks for the token instead, but only from the server we are connected to.
// A link page asks to catch a site's login code (oauth), and then for the address with the code.
const forLinkPages = { oauth: watchLogin, code: loginAddress };

chrome.runtime.onMessageExternal.addListener((message, sender, reply) => {
  if (Object.hasOwn(forLinkPages, message?.type)) {
    forLinkPages[message.type](message, sender).then(reply);
    return true;
  }
  if (message?.type === "ping") {
    serverSettings().then((settings) => reply({ ok: true, connected: settings?.url === sender.origin }));
    return true;
  }
  if (message?.type === "token") {
    serverSettings().then((settings) => reply(settings?.url === sender.origin ? { ok: true, token: settings.token }
                                                                                : { ok: false }));
    return true;
  }
  if (message?.type !== "setup" || typeof message.token !== "string" || !message.token.startsWith("usc_")) return false;
  configure({ url: sender.origin, token: message.token }).then(reply);
  return true;
});
