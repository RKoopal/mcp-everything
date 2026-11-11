"use strict";

const count = document.getElementById("count");
const host = document.getElementById("host");
const result = document.getElementById("result");
const button = document.getElementById("extract");
let recording = null;

function show() {
  count.textContent = `${recording.entries.length} ${recording.entries.length === 1 ? "request" : "requests"}`;
  if (!host.value) host.value = recording.host;
}

window.connect = (shared) => {
  recording = shared;
  recording.onChange = show;
  show();
};

async function extract() {
  const entries = recording.entries.filter((entry) => !host.value || new URL(entry.request.url).host.includes(host.value));
  const sent = await chrome.runtime.sendMessage({ type: "extract", host: host.value || recording.host, entries });
  if (sent && !sent.ok) result.textContent = `Making tools failed: ${sent.error}`;
}

// The background script puts the progress in chrome.storage.session, just as for the popup.
chrome.storage.onChanged.addListener((changes, area) => {
  const extraction = area === "session" && changes.extraction?.newValue;
  if (!extraction) return;
  result.textContent = extraction.state === "running" ? `${extraction.step}…`
    : extraction.state === "failed" ? `Making tools failed: ${extraction.error}`
      : `${extraction.result.added.length} new tools for ${extraction.result.site}: ${extraction.result.added.join(", ") || "none"}`
        + (extraction.result.shared === false ? " (only for you, until the team checked them)" : "");
});

button.addEventListener("click", extract);
chrome.runtime.sendMessage({ type: "server" }).then((server) => {
  button.disabled = !server;
  if (!server) result.textContent = "Connect to your server first: click the extension's icon.";
});
document.getElementById("clear").addEventListener("click", () => {
  recording.entries.length = 0;
  show();
});
