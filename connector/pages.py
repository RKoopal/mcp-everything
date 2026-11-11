"""The pages for people: linking a site (password or code from the site), making an account, logging in from Claude."""
from __future__ import annotations

import secrets
from html import escape

from starlette.responses import HTMLResponse

CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action {form}; frame-ancestors 'none'"
HEADERS = {
    "Content-Security-Policy": CSP.format(form="'self'"),
    # The link in the address bar works once, but stays secret: no referrer to other sites. It does go within this
    # site, because with no-referrer Chrome sends Origin: null with a form, and then the check on /login fails.
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}
STYLE = """
:root { --bg: #f6f5f2; --card: #fff; --text: #1d1d1b; --muted: #6b6a66; --line: #e3e1db; --accent: #1f6f5c;
        --accent-text: #fff; --bad: #a33a2b; color-scheme: light dark; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #171716; --card: #21211f; --text: #ecebe7; --muted: #9c9a94; --line: #34332f; --accent: #5fbfa5;
          --accent-text: #0f1f1a; --bad: #e07a6a; } }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px 16px; background: var(--bg);
       color: var(--text); font: 16px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
main { width: 100%; max-width: 420px; background: var(--card); border: 1px solid var(--line); border-radius: 14px;
       padding: 28px; }
.site { font-size: 13px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); margin: 0 0 4px; }
h1 { font-size: 22px; line-height: 1.25; margin: 0 0 12px; }
p { margin: 0 0 16px; color: var(--muted); }
ol { padding-left: 20px; margin: 0 0 20px; color: var(--muted); } li { margin-bottom: 6px; }
label { display: block; font-size: 14px; font-weight: 600; margin: 0 0 6px; }
input { width: 100%; font: inherit; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--line);
        background: var(--bg); color: var(--text); margin: 0 0 16px; }
input:focus-visible, a:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
button, .button { display: inline-block; width: 100%; text-align: center; font: inherit; font-weight: 600;
                  padding: 11px 14px; border: 0; border-radius: 8px; background: var(--accent);
                  color: var(--accent-text); cursor: pointer; text-decoration: none; }
.secondary { background: transparent; color: var(--accent); border: 1px solid var(--line); margin-bottom: 20px; }
.cancel { margin: 12px 0 0; }
details { margin-top: 16px; } summary { cursor: pointer; }
.error { color: var(--bad); }
.note { font-size: 13px; margin: 16px 0 0; }
code { font-size: 13px; overflow-wrap: anywhere; }
h2 { font-size: 16px; margin: 24px 0 8px; }
.copy { display: block; padding: 10px 12px; margin: 0 0 8px; border: 1px solid var(--line); border-radius: 8px;
        background: var(--bg); color: var(--text); user-select: all; }
.ok { color: var(--accent); font-weight: 600; }
.found-only { display: none; }
body.found .found-only { display: block; }
body.found .missing-only { display: none; }
"""


def page(site: str, title: str, body: str, status: int = 200, data: dict[str, str] | None = None,
         code: str | None = None, form: str = "'self'") -> HTMLResponse:
    """`data` goes as data attributes on <main>, and then `code` (SCRIPT by default) runs too: only that, with a nonce.
    `form`: where a form may go, also after a redirect."""
    csp, script, attributes = CSP.format(form=form), "", ""
    if data is not None:
        nonce = secrets.token_urlsafe(16)
        csp += f"; script-src 'nonce-{nonce}'"
        script = f'<script nonce="{nonce}">{code or SCRIPT}</script>'
        attributes = "".join(f' data-{k}="{escape(v)}"' for k, v in data.items())
    headers = {**HEADERS, "Content-Security-Policy": csp}
    html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{escape(title)}</title><style>{STYLE}</style></head><body><main{attributes}>'
            f'<p class="site">{escape(site)}</p><h1>{escape(title)}</h1>{body}</main>{script}</body></html>')
    return HTMLResponse(html, status_code=status, headers=headers)


def _error(error: str | None) -> str:
    return f'<p class="error" role="alert">{escape(error)}</p>' if error else ""


def _whose(site: str, account: str) -> str:
    """Whose vault a connect link fills. Someone can send you their own link: then you would link your account to theirs."""
    return (f'<p>This links your {escape(site)} account to <strong>{escape(account)}</strong> on {PRODUCT}. '
            "Not your name? Close this page: someone else made this link.</p>") if account else ""


def password(site: str, fields: list[str], error: str | None = None, account: str = "") -> HTMLResponse:
    inputs = "".join(
        f'<label for="{escape(f)}">{escape(f.replace("_", " ").capitalize())}</label>'
        f'<input id="{escape(f)}" name="{escape(f)}" required '
        f'type="{"password" if "pass" in f else "email" if "mail" in f else "text"}" '
        f'autocomplete="{"current-password" if "pass" in f else "username"}">' for f in fields)
    return page(site, "Link your account", (
        f"<p>The connector logs in with this once, and again when your session expires. "
        f"Your details are stored encrypted on the server and only go to {escape(site)}.</p>{_whose(site, account)}"
        f'{_error(error)}<form method="post">{inputs}<button type="submit">Link</button></form>'),
        400 if error else 200)


# Talks to the extension through chrome.runtime: only a page from externally_connectable in its manifest can do that.
TALK = """
const data = document.querySelector("main").dataset;
const talk = (message) => new Promise((resolve) => {
  if (!data.extension || !window.chrome?.runtime?.sendMessage) return resolve(null);
  chrome.runtime.sendMessage(data.extension, message, (reply) => resolve(chrome.runtime.lastError ? null : reply));
});
"""
# A site that gives its code only to its own app (for example appie://login-exit?code=…). With the extension on (it asks every
# 2 s, so turning it on works without a reload), the button lets the extension open the login and catch that address.
# This page asks the extension for it every second, and sends the form with it. Without the extension, you paste it.
OAUTH_SCRIPT = TALK + """
const paste = document.getElementById("paste");
const waiting = document.getElementById("waiting");
const fail = (text) => {
  waiting.hidden = true;
  paste.open = true;
  Object.assign(document.getElementById("failed"), { textContent: text, hidden: false });
};
const look = async () => {
  if (!(await talk({ type: "ping" }))?.ok) return setTimeout(look, 2000);
  document.body.classList.add("found");
  paste.open = false;
};
look();
document.getElementById("extension").addEventListener("click", async () => {
  if (!(await talk({ type: "oauth", authorize: data.authorize, redirect: data.redirect }))?.ok) {
    return fail("The extension did not open the login. Download it again for the newest version, or paste the "
                + "address below.");
  }
  waiting.hidden = false;
  const wait = async () => {
    const reply = await talk({ type: "code" });
    if (!reply?.ok) return fail("The extension stopped waiting. Click the button again, or paste the address below.");
    if (!reply.address) return setTimeout(wait, 1000);
    const form = document.getElementById("link");
    form.elements.code.value = reply.address;
    form.requestSubmit();
  };
  wait();
});
"""


def oauth(site: str, authorize_url: str, redirect: str, extension: str | None = None,
          error: str | None = None, account: str = "") -> HTMLResponse:
    name = escape(site)
    return page(site, "Link your account", (
        f"{_whose(site, account)}{_error(error)}"
        f'<div class="found-only"><p>The extension opens the login of {name} in a new tab. Log in '
        "there: this page links your account by itself.</p>"
        f'<button type="button" id="extension">Log in to {name}</button>'
        f'<p id="waiting" class="note" role="status" hidden>Waiting until you have logged in to {name}…</p></div>'
        '<p id="failed" class="error" role="alert" hidden></p>'
        f'<div class="missing-only"><p>Turn on the {PRODUCT} extension, then you do not have to paste anything. '
        "This page sees it as soon as it is on.</p>"
        f'<details><summary class="note">No extension yet?</summary>{INSTALL}</details></div>'
        '<details id="paste" open><summary>Paste the address yourself</summary>'
        f'<ol><li>Log in to {name}. Open the developer tools first (Network, with "Preserve log").</li>'
        f"<li>After you log in, the site opens an address that starts with <code>{escape(redirect)}</code>. "
        f"Copy that address from the list.</li><li>Paste it below.</li></ol>"
        f'<a class="button secondary" href="{escape(authorize_url)}" target="_blank" rel="noopener noreferrer">'
        f"Log in to {name}</a>"
        f'<form id="link" method="post"><label for="code">Address or code</label>'
        f'<input id="code" name="code" required autocomplete="off" placeholder="{escape(redirect)}?code=…">'
        f'<button type="submit">Link</button></form></details>'
        f'<p class="note">The connector only keeps the access token, encrypted.</p>'),
        400 if error else 200, data={"extension": extension or "", "authorize": authorize_url, "redirect": redirect},
        code=OAUTH_SCRIPT)


def done(site: str) -> HTMLResponse:
    return page(site, "Linked", "<p>You can close this tab and go on in your chat.</p>")


def expired() -> HTMLResponse:
    return page("connector", "This link no longer works",
                "<p>A connect link works for ten minutes, and once. Ask for a new one in your chat.</p>", 410)


# ---------- making an account ----------

PRODUCT = "MCP Everything"
# It asks every 2 s until the extension answers: Chrome gives an open page chrome.runtime as soon as the
# extension is installed, so nobody reloads, and installing before or after making the account both work. With a
# token (the account is made) the extension gets it once it answers. Then the copy buttons.
SCRIPT = TALK + """
const { token } = data;
const look = async () => {
  if (!(await talk({ type: "ping" }))?.ok) return setTimeout(look, 2000);
  const reply = token ? await talk({ type: "setup", token }) : { ok: true };
  document.body.classList.toggle("found", Boolean(reply?.ok));
  const failed = document.getElementById("failed");
  if (failed && !reply?.ok) { failed.textContent = reply?.error ?? "Could not connect."; failed.hidden = false; }
};
look();
for (const button of document.querySelectorAll("[data-copy]")) {
  button.addEventListener("click", async () => {
    await navigator.clipboard.writeText(document.getElementById(button.dataset.copy).textContent);
    button.textContent = "Copied";
  });
}
"""
INSTALL = ('<a class="button secondary" href="/extension.zip">Download the extension</a>'
           "<ol><li>Unzip it.</li><li>Open <code>chrome://extensions</code> and turn on "
           "<strong>Developer mode</strong> at the top right.</li><li>Click <strong>Load unpacked</strong> and choose "
           "the folder <code>mcp-everything-extension</code>.</li><li>Pin the extension with the puzzle piece.</li></ol>")


def signup(extension: str | None, error: str | None = None) -> HTMLResponse:
    return page(PRODUCT, "Create your account", (
        "<p>Record a website once with the Chrome extension. The server makes tools from it, and your "
        "AI agent uses them in Claude.</p>"
        "<h2>1. Install the extension</h2>"
        '<p class="found-only ok">✓ The extension is in Chrome.</p>'
        f'<div class="missing-only">{INSTALL}<p>This page sees it as soon as it is installed. You can also do this '
        "after the next step.</p></div>"
        "<h2>2. Pick your name and password</h2>"
        f'{_error(error)}<form method="post"><label for="name">Name</label>'
        '<input id="name" name="name" required minlength="2" maxlength="32" autocomplete="username" '
        'autocapitalize="none" spellcheck="false" placeholder="for example anna">'
        '<label for="new-password">Pick a password</label>'
        '<input id="new-password" name="password" type="password" required minlength="10" maxlength="200" '
        'autocomplete="new-password"><p class="note">At least 10 characters. You log in with your name and this '
        "password.</p>"
        '<button type="submit">Create account</button></form>'
        '<p class="note">The server keeps your name and which sites you record.</p>'),
        400 if error else 200, data={"extension": extension or ""})


def connect_claude(public_url: str) -> str:
    """How you connect Claude. Claude then asks you to log in itself: that is this server, with one click."""
    command = f"claude mcp add --transport http mcp-everything {public_url}/mcp"
    return ("<h2>Connect Claude</h2>"
            "<p>In Claude (web or desktop): in <strong>Settings → Connectors</strong>, add a custom connector "
            "with this address. Claude then asks you to log in: that is one click.</p>"
            f'<code id="address" class="copy">{escape(public_url)}/mcp</code>'
            '<button class="secondary" type="button" data-copy="address">Copy address</button>'
            "<p>In Claude Code: paste this command in your terminal, then type <code>/mcp</code> and choose "
            "<strong>Authenticate</strong>.</p>"
            f'<code id="command" class="copy">{escape(command)}</code>'
            '<button class="secondary" type="button" data-copy="command">Copy command</button>')


def _extension(token: str, once: bool = True) -> str:
    """The extension on a welcome page: ✓ once SCRIPT gave it the token, until then how to install it. And the token
    itself, for when the extension does not answer: `once` for a new account, where the page shows it only this time."""
    return ('<p class="found-only ok">✓ Your extension is connected to your account.</p>'
            '<p id="failed" class="error" role="alert" hidden></p>'
            f'<div class="missing-only">{INSTALL}<p>This page connects the extension to your account as soon as it '
            f"is installed. If it does not, click its icon and paste this token.{' You see it only once.' if once else ''}</p>"
            f'<code id="token" class="copy">{escape(token)}</code>'
            '<button class="secondary" type="button" data-copy="token">Copy token</button></div>')


def welcome(name: str, token: str, public_url: str, extension: str | None) -> HTMLResponse:
    return page(PRODUCT, f"Welcome, {name}", (
        f"{_extension(token)}"
        "<h2>Record a site</h2>"
        "<p>Open a site, click the extension's icon and then <strong>Start recording</strong>. Do what your "
        "agent should be able to do, and click <strong>Stop and make tools</strong>. When the extension is done, "
        "ask Claude.</p>"
        f"{connect_claude(public_url)}"),
        data={"extension": extension or "", "token": token})


ABOUT_STYLE = """
:root { --bg: #0b1021; --line: rgba(255, 255, 255, .14); --text: rgba(255, 255, 255, .62); color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 32px 20px; background: var(--bg);
       color: var(--text); font: 13px/1 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
main { width: 100%; max-width: 1100px; }
.frame { position: relative; cursor: pointer; }
video { display: block; width: 100%; border-radius: 12px; box-shadow: 0 0 0 1px rgba(255, 255, 255, .06); }
.big { position: absolute; inset: 0; margin: auto; width: 76px; height: 76px; border: 0; border-radius: 50%; cursor: pointer;
       background: rgba(255, 255, 255, .12); backdrop-filter: blur(8px); display: grid; place-items: center;
       transition: opacity .25s, transform .25s; }
.big:hover { transform: scale(1.06); background: rgba(255, 255, 255, .18); }
.playing .big { opacity: 0; pointer-events: none; }
.bar { display: flex; align-items: center; gap: 14px; margin-top: 14px; opacity: .55; transition: opacity .25s; }
main:hover .bar, .bar:focus-within, .paused .bar { opacity: 1; }
.bar button { border: 0; padding: 4px; background: none; cursor: pointer; display: grid; place-items: center; }
.track { flex: 1; height: 14px; display: flex; align-items: center; cursor: pointer; }
.track div { width: 100%; height: 2px; border-radius: 2px; background: var(--line); transition: height .15s; }
.track:hover div { height: 4px; }
.track span { display: block; height: 100%; width: 0; border-radius: 2px; background: #5fbfa5; }
time { font-variant-numeric: tabular-nums; min-width: 84px; text-align: right; }
svg { width: 18px; height: 18px; fill: rgba(255, 255, 255, .85); }
.big svg { width: 26px; height: 26px; margin-left: 4px; fill: #fff; }
button:focus-visible, .track:focus-visible { outline: 2px solid #5fbfa5; outline-offset: 3px; border-radius: 6px; }
"""
PLAY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4.5v15l13-7.5z"/></svg>'
PAUSE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4h4v16H6zm8 0h4v16h-4z"/></svg>'
FULL = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h6v2H6v4H4zm10 0h6v6h-2V6h-4zM4 14h2v4h4v2H4zm14 '
        '0h2v6h-6v-2h4z"/></svg>')
# Our own controls, under the video: the browser's bar would cover the captions in the video.
ABOUT_SCRIPT = """
const main = document.querySelector("main"), video = main.querySelector("video");
const toggle = main.querySelector(".toggle"), track = main.querySelector(".track"), fill = track.querySelector("span");
const time = main.querySelector("time"), icons = { play: toggle.innerHTML, pause: main.dataset.pause };
const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const flip = () => video.paused ? video.play() : video.pause();
const show = () => {
  main.classList.toggle("playing", !video.paused); main.classList.toggle("paused", video.paused);
  toggle.innerHTML = video.paused ? icons.play : icons.pause;
  toggle.setAttribute("aria-label", video.paused ? "Play" : "Pause");
};
const tick = () => {
  const d = video.duration || 0;
  fill.style.width = d ? `${(video.currentTime / d) * 100}%` : "0";
  time.textContent = `${clock(video.currentTime)} / ${clock(d)}`;
  track.setAttribute("aria-valuenow", Math.round(video.currentTime));
  track.setAttribute("aria-valuemax", Math.round(d));
};
main.querySelector(".frame").addEventListener("click", flip);
toggle.addEventListener("click", flip);
main.querySelector(".full").addEventListener("click", () => video.requestFullscreen?.());
for (const e of ["play", "pause", "ended"]) video.addEventListener(e, show);
for (const e of ["timeupdate", "loadedmetadata", "seeked"]) video.addEventListener(e, tick);
track.addEventListener("click", (e) => {
  const box = track.getBoundingClientRect();
  video.currentTime = ((e.clientX - box.left) / box.width) * video.duration;
});
document.addEventListener("keydown", (e) => {
  if (e.key === " " || e.key === "k") { if (e.target.tagName !== "BUTTON") { e.preventDefault(); flip(); } }
  else if (e.key === "ArrowRight") video.currentTime += 5;
  else if (e.key === "ArrowLeft") video.currentTime -= 5;
  else if (e.key === "f") video.requestFullscreen?.();
});
show(); tick();
"""


def about() -> HTMLResponse:
    """The promo video, on its own. The file is on the server only (`<data>/about.mp4`), not in git."""
    nonce = secrets.token_urlsafe(16)
    csp = (f"default-src 'none'; style-src 'unsafe-inline'; media-src 'self'; script-src 'nonce-{nonce}'; "
           "form-action 'none'; frame-ancestors 'none'")
    html = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{PRODUCT}</title><style>{ABOUT_STYLE}</style></head><body>"
            f'<main class="paused" data-pause="{escape(PAUSE)}">'
            '<div class="frame"><video src="/about.mp4" playsinline preload="auto"></video>'
            f'<button class="big" type="button" aria-label="Play">{PLAY}</button></div>'
            f'<div class="bar"><button class="toggle" type="button" aria-label="Play">{PLAY}</button>'
            '<div class="track" role="slider" tabindex="0" aria-label="Position" aria-valuemin="0"><div><span></span>'
            f'</div></div><time>0:00</time><button class="full" type="button" aria-label="Full screen">{FULL}</button>'
            f"</div></main><script nonce=\"{nonce}\">{ABOUT_SCRIPT}</script></body></html>")
    return HTMLResponse(html, headers={**HEADERS, "Content-Security-Policy": csp})


def closed() -> HTMLResponse:
    return page(PRODUCT, "This invite no longer works",
                "<p>It was used already, or the server is full. Ask for a new one.</p>", 410)


# ---------- logging in from Claude ----------

# Asks the extension if it is connected to this server. A click on the button gets the token from it and sends
# the form. Only this server's pages can ask that (externally_connectable).
LOGIN_SCRIPT = TALK + """
talk({ type: "ping" }).then((reply) => document.body.classList.toggle("found", Boolean(reply?.ok && reply.connected)));
document.getElementById("extension")?.addEventListener("click", async () => {
  const reply = await talk({ type: "token" });
  if (!reply?.token) {
    document.body.classList.remove("found");
    const failed = document.getElementById("failed");
    failed.textContent = "The extension gave no token. Paste it below, or open the extension and connect again.";
    failed.hidden = false;
    return;
  }
  const form = document.getElementById("login");
  form.elements.token.value = reply.token;
  form.requestSubmit();  // without a button: the server reads that as logging in
});
"""


def login(client: str, back_to: str, extension: str | None, form: str, user: str | None = None,
          error: str | None = None, invite: str = "", new: str = "", who: str = "") -> HTMLResponse:
    """`user`: who this browser already knows (the cookie). Otherwise: the extension, name and password (or the
    token), or making an account with an invite. `who`, `invite` and `new` are what was typed there before."""
    paste = ('<label for="login-name">Name</label>'
             '<input id="login-name" name="user" autocomplete="username" autocapitalize="none" spellcheck="false" '
             f'value="{escape(who)}">'
             '<label for="password">Password</label>'
             '<input id="password" name="password" type="password" autocomplete="current-password">'
             '<button type="submit" name="action" value="allow">Log in</button>'
             '<details><summary class="note">Log in with your token instead</summary>'
             '<label for="token">Your token</label>'
             '<input id="token" name="token" autocomplete="off" spellcheck="false" placeholder="usc_…">'
             '<button class="secondary" type="submit" name="action" value="allow">Log in with the token</button>'
             '<p class="note">You got the token with your account; it is also in the extension.</p></details>')
    signup = ""
    if user:
        choice = (f'<button type="submit" name="action" value="allow">Continue as {escape(user)}</button>'
                  f'<details><summary class="note">A different account</summary>{paste}</details>')
    else:
        choice = ('<div class="found-only"><button type="button" id="extension">'
                  "Log in with the extension</button></div>"
                  '<p id="failed" class="error" role="alert" hidden></p>'
                  f'<div class="missing-only"><h2>Have an account?</h2>{paste}</div>')
        signup = ('<div class="missing-only"><h2>New here?</h2><p>Make your account with the invite you got.</p>'
                  '<form method="post"><input type="hidden" name="action" value="signup">'
                  '<label for="invite">Invite code or link</label>'
                  f'<input id="invite" name="invite" required autocomplete="off" spellcheck="false" '
                  f'value="{escape(invite)}" placeholder="https://…/signup/…">'
                  '<label for="name">Pick a name</label>'
                  '<input id="name" name="name" required minlength="2" maxlength="32" autocomplete="username" '
                  f'autocapitalize="none" spellcheck="false" value="{escape(new)}" placeholder="for example anna">'
                  '<label for="new-password">Pick a password</label>'
                  '<input id="new-password" name="password" type="password" required minlength="10" maxlength="200" '
                  'autocomplete="new-password"><p class="note">At least 10 characters. You log in with your name and this '
                  "password.</p>"
                  '<button type="submit">Create account</button></form></div>')
    return page(PRODUCT, "Log in", (
        f"<p><strong>{escape(client)}</strong> wants to use the tools of {PRODUCT}, on your behalf. "
        f"Then you go back to <strong>{escape(back_to)}</strong>.</p>"
        f'{_error(error)}<form id="login" method="post">{choice}</form>{signup}'
        '<form method="post"><button class="secondary cancel" type="submit" name="action" value="deny">Cancel</button>'
        "</form>"),
        400 if error else 200, data={"extension": extension or ""}, code=LOGIN_SCRIPT, form=form)


def login_welcome(client: str, back_to: str, name: str, token: str, extension: str | None, form: str,
                  new: bool = True) -> HTMLResponse:
    """Just made an account on the login page (`new`), or logged in with a pasted token: then this browser may not have
    the extension yet. The request from Claude is still open, and the cookie knows the user: "Continue" is the same as
    "Continue as" on the login page."""
    return page(PRODUCT, f"Welcome, {name}" if new else f"Logged in as {name}", (
        f"<p>{'Your account is ready. ' if new else ''}The extension records the sites you want tools for; you can "
        "also install it later.</p>"
        f"<h2>1. Install the extension</h2>{_extension(token, once=new)}"
        f"<h2>2. Back to {escape(client)}</h2>"
        f"<p>Then you go back to <strong>{escape(back_to)}</strong>.</p>"
        f'<form method="post"><button type="submit" name="action" value="allow">Continue to {escape(client)}'
        "</button></form>"
        '<p class="note">This works for ten minutes. After that, connect again from Claude: this browser remembers '
        "you, so it is one click.</p>"),
        data={"extension": extension or "", "token": token}, form=form)


def login_done(client: str, user: str | None, extension: str | None) -> HTMLResponse:
    """The login page loaded again after this request logged in: say so, not that it no longer works."""
    again = (f"This browser remembers you as <strong>{escape(user)}</strong>, so that is one click."
             if user else "")
    return page(PRODUCT, "You are logged in", (
        f"<p>You logged in to <strong>{escape(client)}</strong> with this request. Go back to {escape(client)}.</p>"
        f"<p>Does {escape(client)} not show the connection? Connect again from there. {again}</p>"
        f'<div class="missing-only"><h2>No extension yet?</h2><p>It records the sites you want tools for.</p>'
        f"{INSTALL}<p>Then click its icon and paste your token.</p></div>"),
        410, data={"extension": extension or ""}, code=LOGIN_SCRIPT)


def login_expired(user: str | None = None) -> HTMLResponse:
    """`user`: who this browser knows, for example because the account was made while the request ran out."""
    known = (f"<p>Your account is there, and this browser remembers you as <strong>{escape(user)}</strong>: the "
             "next try is one click.</p>") if user else ""
    return page(PRODUCT, "This login request no longer works",
                f"<p>A login request works for ten minutes, and once. Try again from Claude.</p>{known}", 410)
