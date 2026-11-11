// background.js opens this page for a link page of a site that gives its code only to its own app. Chrome asks for
// access to a site only after a click in the extension itself, so this page has the button. With access already
// given, it goes to the login at once.
"use strict";

const { hostOf, patternFor } = globalThis.connectorSummary;
const button = document.getElementById("allow");
const fail = (text) => Object.assign(document.getElementById("failed"), { textContent: text, hidden: false });

chrome.storage.session.get("login").then(async ({ login }) => {
  if (!login) {
    button.hidden = true;
    return fail("Nothing to log in to. Start again on the link page.");
  }
  const { hostname } = new URL(login.authorize);
  const origins = [patternFor(hostname)];
  for (const host of document.querySelectorAll(".host")) host.textContent = hostOf(hostname);
  // catch.js on the site until the code is there (background.js removes it then), and only in this browser session.
  const go = async () => {
    await chrome.scripting.unregisterContentScripts({ ids: ["login"] }).catch(() => {});
    await chrome.scripting.registerContentScripts([{ id: "login", matches: origins, js: ["catch.js"],
                                                     runAt: "document_start", persistAcrossSessions: false }]);
    location.replace(login.authorize);
  };
  if (await chrome.permissions.contains({ origins })) return go();
  button.addEventListener("click", async () => {
    if (await chrome.permissions.request({ origins })) return go();
    fail("Without access the extension cannot read the address. Paste it on the link page instead.");
  });
});
