// On a site's login page while a link page waits for its code (login.js puts it there). Many sites go to their app's
// address (for example appie://login-exit?code=…) with JavaScript after you log in: no request, so webRequest does
// not see it, but the navigate event does. background.js keeps only the address the link page waits for.
"use strict";

navigation.addEventListener("navigate", (event) => {
  const { url } = event.destination;
  if (!/^(https?|about|blob|data|javascript):/i.test(url)) chrome.runtime.sendMessage({ type: "caught", url });
});
