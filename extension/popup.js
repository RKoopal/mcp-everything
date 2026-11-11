"use strict";

const { hostOf, patternFor } = globalThis.connectorSummary;
const $ = (id) => document.getElementById(id);
const DEFAULT_SERVER = "https://mcp-everything.com";
const [site, trace, statusLine, known, recent, action, linkButton, extractionBox, settings, serverName, serverToggle] =
  ["site", "trace", "status", "known", "recent", "action", "link", "extraction", "settings", "server-name",
   "server-toggle"].map($);
const send = (type, fields = {}) => chrome.runtime.sendMessage({ type, ...fields });
let server = null;
let refreshKnown = () => {};
let ticking = null;

// First say what must happen, only then ask for permission: Chrome often closes the popup as soon as it
// asks. The background script then does it itself, once permission is given. Returns false on a no.
function withAccess(origins, next) {
  const done = send("arm", { origins, next });
  return chrome.permissions.request({ origins }).then((granted) => granted && done);
}

// Chrome reads the popup from disk every time, but the background script only after a reload. If the version on
// disk differs from the one Chrome runs, or the background script does not answer, the popup says so, and the button
// opens chrome://extensions at this extension. Not chrome.runtime.reload(): that makes an unpacked extension disappear
// instead of reloading it (tested in Chrome 156).
function askReload(text) {
  $("update-text").textContent = `${text} Click ↻ on this extension in chrome://extensions.`;
  $("update").hidden = false;
  $("reload").onclick = () => chrome.tabs.create({ url: `chrome://extensions/?id=${chrome.runtime.id}` });
}

async function checkVersion() {
  const running = chrome.runtime.getManifest().version;
  const { version } = await fetch(chrome.runtime.getURL("manifest.json")).then((r) => r.json());
  if (version !== running) askReload(`Version ${version} is ready, but Chrome still runs ${running}.`);
}

function setStatus(text) {
  statusLine.textContent = text;
}

function setCount(count) {
  const number = document.createElement("strong");
  number.textContent = count;
  statusLine.replaceChildren(number, ` ${count === 1 ? "request" : "requests"} recorded`);
}

function setButton(label, handler) {
  action.textContent = label;
  action.hidden = !label;
  action.onclick = handler;
}

function list(element, names) {
  element.replaceChildren(...names.map((name) => {
    const item = document.createElement("li");
    item.textContent = name;
    item.title = name;
    return item;
  }));
}

function paragraph(text) {
  const line = document.createElement("p");
  line.textContent = text;
  return line;
}

function draw(ticks) {
  trace.replaceChildren(...ticks.map((kind) => {
    const tick = document.createElement("i");
    tick.className = kind;
    return tick;
  }));
}

function showRecording(recording) {
  document.body.classList.add("recording");
  site.textContent = recording.host;
  draw(recording.ticks);
  setCount(recording.count);
  list(recent, recording.recent);
  setButton("Stop and make tools", stop);
  serverToggle.hidden = true;  // disconnecting during a recording throws the recording away
}

async function stop() {
  document.body.classList.remove("recording");
  recent.replaceChildren();
  setButton("");
  const done = await send("stop");
  setStatus(`${done.count} ${done.count === 1 ? "request" : "requests"} sent to the server.`);
  serverToggle.hidden = false;
}

const elapsed = (since) => {
  const seconds = Math.max(0, Math.round((Date.now() - since) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
};

function showExtraction(extraction) {
  clearInterval(ticking);
  if (!extraction) return;
  extractionBox.hidden = false;
  extractionBox.className = extraction.state;
  if (extraction.state === "running") {
    const step = paragraph("");
    const tick = () => { step.textContent = `${extraction.step}… ${elapsed(extraction.started)}`; };
    tick();
    ticking = setInterval(tick, 1000);
    extractionBox.replaceChildren(paragraph(`Making tools for ${extraction.host}`), step);
    return;
  }
  if (extraction.state === "failed") {
    extractionBox.replaceChildren(paragraph(`Making tools for ${extraction.host} failed: ${extraction.error}`));
    return;
  }
  const { site: id, added, session, shared = true } = extraction.result;
  const tools = document.createElement("ul");
  list(tools, added);
  const copy = document.createElement("button");
  copy.className = "secondary";
  copy.textContent = "Copy command for Claude Code";
  copy.title = "claude mcp add …, once per computer. Then type /mcp in Claude Code and log in.";
  copy.onclick = async () => {
    await navigator.clipboard.writeText(await send("claude"));
    copy.textContent = "Copied. Paste it in your terminal, then log in with /mcp.";
  };
  if (!added.length) {
    extractionBox.replaceChildren(paragraph(`Done, but no new tools for ${id}. Try other actions than last time.`));
    return;
  }
  extractionBox.replaceChildren(
    paragraph(`Done: ${added.length} new ${added.length === 1 ? "tool" : "tools"} for ${id}${session ? ", session linked" : ""}.`),
    tools,
    ...(shared ? [] : [paragraph("Only you see them until the team has checked them. Then everyone can use them.")]),
    paragraph("Now ask Claude to do what you just did: in a new chat, or in Claude Code after /mcp and Reconnect."),
    copy,
  );
}

function showKnown(found, host) {
  known.hidden = false;
  if (!found) {
    known.textContent = `The server does not know ${host} yet. Record what you want to be able to do.`;
    return;
  }
  const count = document.createElement("strong");
  count.textContent = `${found.tools.length} tools`;
  const state = found.login === "none" ? "no login needed" : found.connected ? "linked" : "not linked";
  known.replaceChildren(count, ` · ${state}`);
  linkButton.hidden = !["session", "password"].includes(found.login);
  linkButton.textContent = found.connected ? "Link session again" : "Link session";
}

function showSetup() {
  setStatus("Not connected yet. Open the invite link you got: the extension then connects by itself. "
    + "Have a token already? Paste it below.");
  settings.hidden = false;
  $("token").focus();
}

function showIdle(tab) {
  const url = tab?.url ? new URL(tab.url) : null;
  if (!url || !url.protocol.startsWith("http")) {
    setStatus("First open the site you want to record.");
    return;
  }
  const host = hostOf(url.hostname);
  const origins = [patternFor(url.hostname)];
  site.textContent = host;
  setStatus("Records this tab and sends it to your server.");
  setButton("Start recording", async () => {
    if (await withAccess(origins, { type: "start", tabId: tab.id, url: tab.url, host }) === false) {
      setStatus(`No access to ${host}. Click again and choose Allow.`);
      return;
    }
    window.close();
  });
  linkButton.onclick = async () => {
    if (await withAccess(origins, { type: "link", url: tab.url }) === false) setStatus(`No access to the cookies of ${host}.`);
  };
  refreshKnown = () => send("lookup", { host }).then((found) => found.ok ? showKnown(found.site, host) : setStatus(found.error));
  refreshKnown();
}

function showServer() {
  serverName.textContent = server ? new URL(server.url).host : "No server";
  serverToggle.textContent = server ? "Disconnect" : "Set up";
  serverToggle.onclick = server
    ? async () => { await send("disconnect"); window.location.reload(); }
    : () => { settings.hidden = !settings.hidden; if (!settings.hidden) $("url").focus(); };
}

$("url").value = DEFAULT_SERVER;
settings.addEventListener("submit", async (event) => {
  event.preventDefault();
  let origin;
  try {
    origin = new URL($("url").value).origin;
  } catch {
    setStatus("That is not a valid address.");
    return;
  }
  setStatus("Connecting…");
  await send("configure", { url: origin, token: $("token").value.trim() });  // the outcome comes as a notice
});

// The background script writes every change to chrome.storage.session; the popup draws along.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "session") return;
  if (changes.notice?.newValue) window.location.reload();
  if (changes.recording?.newValue) showRecording(changes.recording.newValue);
  const extraction = changes.extraction?.newValue;
  if (extraction) {
    showExtraction(extraction);
    if (extraction.state === "done") refreshKnown();
  }
});

(async () => {
  const [settingsNow, recording, extraction, { notice }] = await Promise.all([
    send("server"), send("status"), send("extracted"), chrome.storage.session.get("notice")]);
  server = settingsNow;
  showServer();
  showExtraction(extraction);
  if (recording) {
    showRecording(recording);
  } else if (!server) {
    showSetup();
  } else {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    showIdle(tab);
  }
  if (notice) {
    setStatus(notice.text);
    await chrome.storage.session.remove("notice");
  }
  // A reload wipes a running recording: then wait until next time.
  if (!recording && extraction?.state !== "running") checkVersion();  // if that fails, no notice
})().catch(() => askReload("The extension does not respond."));
