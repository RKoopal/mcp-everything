// Runs while DevTools is open, even if the panel is never opened.
// Raw content stays here in memory and goes away with DevTools, unless you send it to the server.
// Passwords are masked at once, as in the recording from the popup: they never go to the server.
"use strict";

const { API_TYPES, maskPassword, template } = globalThis.connectorSummary;

const recording = { entries: [], page: null, url: "", host: "", onChange: () => {} };
globalThis.recording = recording;

chrome.devtools.network.onNavigated.addListener((address) => {
  const url = new URL(address);
  recording.url = address;
  recording.page = template(url.pathname);
  recording.host = url.host.replace(/^www\./, "");
  recording.onChange();
});

chrome.devtools.network.onRequestFinished.addListener((request) => {
  const mimeType = request.response.content.mimeType || "";
  const entry = {
    request: {
      url: request.request.url,
      method: request.request.method,
      headers: request.request.headers,
      queryString: request.request.queryString,
      postData: { text: maskPassword(request.request.postData?.text) },
    },
    response: { status: request.response.status, content: { mimeType } },
    page: recording.page,
  };
  recording.entries.push(entry);
  recording.onChange();
  if (API_TYPES.some((type) => mimeType.toLowerCase().includes(type))) {
    request.getContent((text, encoding) => {
      entry.response.content.text = text;
      if (encoding) entry.response.content.encoding = encoding;
    });
  }
});

chrome.devtools.panels.create("Connector", "", "panel.html", (panel) => {
  panel.onShown.addListener((panelWindow) => panelWindow.connect(recording));
});
