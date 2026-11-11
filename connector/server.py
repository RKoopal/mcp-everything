"""The server: one MCP connection for all sites. Start it with `python -m connector.server`.

- POST /mcp              MCP (streamable HTTP). Claude logs in with OAuth (see `connector.oauth`); an API token from
                         `python -m connector.users add` works too, as Bearer.
- /authorize, /token, /register, /revoke, /.well-known/…   OAuth 2.1 for MCP, from the SDK.
- GET  /login/<request>  Logging in from Claude: with the extension, the cookie from an earlier time, or your token.
                         New here: an invite makes the account on this page, and it offers the extension.
- POST /api/recordings   The extension sends a recording; the pipeline makes tools from it, for everyone.
                         That takes a while: the answer is an id, and the pipeline runs in the background.
- GET  /api/recordings/<id>  How far making tools is, and then the result.
- POST /api/sessions     The extension links a site's session, for this user.
- GET  /api/sites        What exists and what this user linked.
- GET  /connect/<link>   Logging in with a password or at the site (OAuth), through a one-time link.
- GET  /signup/<code>    Making an account yourself, with an invite from `python -m connector.users invite`. The login
                         page does the same; this link stays for invites that point here.
- GET  /extension.zip    The Chrome extension, with this server's address already filled in.
- GET  /about           The promo video: `<data>/about.mp4`, put there by hand.
- GET  /healthz

Tools are named <site>_<tool>. Two are always there: `sites` and `connect`. Tools belong to everyone;
sessions, passwords and the write log are per user and encrypted.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import logging.handlers
import os
import re
import secrets
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import anthropic
import anyio
import httpx2
import jsonschema
import mcp_types as types
import uvicorn
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import build_resource_metadata_url, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from . import auth, oauth, pages, pipeline
from .auth import SessionAuth
from .browser import Browser
from .errors import AuthExpired, ConnectorError
from .registry import Registry, host_of, visible
from .runner import Runner, http_client
from .store import Store
from .users import SignupError, Users

log = logging.getLogger("connector")
logins = logging.getLogger("connector.login")  # each login on /login: how it went, which name, from where

MAX_RECORDING = 25_000_000
MAX_RESULT = 60_000
LINK_TTL = 600
LINK_ATTEMPTS = 5
JOB_TTL = 3600
SCRYPT_AT_ONCE = 4
MONTH = 30 * 86_400
EXTENSION = Path(__file__).resolve().parent.parent / "extension"
EXTENSION_FILES = {".js", ".json", ".html", ".md", ".png"}
KEY = {"type": "string", "minLength": 8, "maxLength": 100,
       "description": "One key per intended action. Use the same key when you retry the same action; "
                      "then it never happens twice."}
CONFIRM = {"type": "object", "properties": {"confirm": {"type": "boolean", "title": "Yes, do this"}},
           "required": ["confirm"]}
TRIES = 5  # wrong passwords per name per ten minutes, on the login page
INSTRUCTIONS = """One connection to many websites, on behalf of this user. Tools are named <site>_<tool>.
- `sites` shows which sites exist and which are linked. If a tool says the session is missing or \
expired, use `connect` and give the user what that tool returns.
- A write asks for an idempotency_key: the same one when you retry, a new one for a new action.
- The user confirms some writes themselves; the server then asks for that.
- A site that is not here: the user records it with the browser extension. Then its tools are here."""


def install_extension(public_url: str) -> str:
    """For Claude, to give the user: where the extension is, and how to install it."""
    return (f"No extension yet? It is at {public_url}/extension.zip: unzip it, open chrome://extensions, turn on "
            "Developer mode, click Load unpacked and choose the folder mcp-everything-extension. Then click its icon "
            "and paste the user's token (usc_…).")


@dataclass(frozen=True)
class Settings:
    data: Path
    key: bytes
    public_url: str = "http://127.0.0.1:8000"
    browser_url: str | None = None
    allow_private: bool = False  # only for tests and a demo on your own machine
    # Limits for who came through an invite; an account from an admin has none. Together they also
    # cap the cost: at most max_users × recordings_per_month calls to Claude per 30 days.
    max_users: int = 25
    recordings_per_month: int = 5
    # On: only a guest sees the guest's tools, until a review. Off while you know the guests: then they are
    # for everyone at once, and a waiting tool does not block someone else's recording.
    review_guest_tools: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        try:
            key = base64.b64decode(env.get("CONNECTOR_SECRET_KEY", ""), validate=True)
        except ValueError:
            key = b""
        if len(key) != 32:
            raise SystemExit("CONNECTOR_SECRET_KEY must be 32 bytes, in base64. Make one with: openssl rand -base64 32")
        return cls(data=Path(env.get("DATA_DIR", "data")), key=key,
                   public_url=env.get("PUBLIC_URL", cls.public_url).rstrip("/"),
                   browser_url=env.get("BROWSER_URL") or None,
                   max_users=int(env.get("MAX_USERS", cls.max_users)),
                   recordings_per_month=int(env.get("MAX_RECORDINGS_PER_MONTH", cls.recordings_per_month)),
                   review_guest_tools=env.get("REVIEW_GUEST_TOOLS") == "1",
                   allow_private=env.get("ALLOW_PRIVATE_SITES") == "1")


@dataclass
class Link:
    user: dict
    site: str
    expires: float
    attempts: int = 0


@dataclass
class Job:
    """Making tools from one recording, in the background. The extension asks for the state with the id."""
    user: str
    host: str
    step: str = "Recording received"
    result: dict | None = None
    error: str | None = None
    finished: float | None = None  # set last: only then are result or error final

    def view(self) -> dict:
        state = "running" if self.finished is None else "failed" if self.error else "done"
        return {"state": state, "step": self.step, "host": self.host,
                **({"result": self.result} if state == "done" else {}), **({"error": self.error} if self.error else {})}


@dataclass
class Connector:
    """Everything the server does, without HTTP. Synchronous: the routes run this in a thread, making tools in its own."""
    settings: Settings
    ask: Callable[[dict], pipeline.Proposal] = pipeline.ask_claude
    links: dict[str, Link] = field(default_factory=dict)
    jobs: dict[str, Job] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        self.users = Users(self.settings.data)
        self.registry = Registry(self.settings.data / "registry")
        self.seal_key = hmac.new(self.settings.key, b"confirm", hashlib.sha256).digest()  # not the vault key itself

    # ---------- per user ----------

    def store(self, user: dict, site: str) -> Store:
        return Store(site, self.settings.data / "vault" / user["id"], self.settings.key)

    def manifest(self, site: str) -> dict:
        if not (manifest := self.registry.get(site)):
            raise ConnectorError(f"Unknown site: {site}. See the sites tool.")
        return manifest

    @contextmanager
    def runner(self, user: dict, site: str) -> Iterator[Runner]:
        manifest = visible(self.manifest(site), user["id"])
        if manifest.get("runtime") == "browser":
            if not self.settings.browser_url:
                raise ConnectorError(f"{site} needs the browser runtime, and it is not on for this server.")
            http = Browser(self.settings.browser_url, manifest["base"], self.settings.allow_private)
        else:
            http = http_client(manifest["base"], self.settings.allow_private)
        with http:
            yield Runner(manifest, self.store(user, site), http)

    def sites(self, user: dict) -> list[dict]:
        listed = []
        for site in self.registry.ids():
            manifest = visible(self.registry.get(site), user["id"])
            listed.append({"site": site, "base": manifest["base"], "login": manifest.get("auth", {}).get("type", "none"),
                           "runtime": manifest.get("runtime", "http"),
                           "connected": auth.for_manifest(manifest, self.store(user, site)).connected(),
                           "tools": [f"{site}_{t['name']}" for t in manifest["tools"]]})
        return listed

    # ---------- tools ----------

    def tool(self, user: dict, name: str) -> tuple[str, dict]:
        site, _, tool = name.partition("_")
        manifest = self.registry.get(site) if site and tool else None
        manifest = visible(manifest, user["id"]) if manifest else None
        found = next((t for t in manifest["tools"] if t["name"] == tool), None) if manifest else None
        if not found:
            raise ConnectorError(f"Unknown tool: {name}.")
        return site, found

    def call(self, user: dict, site: str, tool: dict, args: dict, confirmed: bool = False):
        with self.runner(user, site) as runner:
            if tool["effect"] == "write":
                key = args.pop("idempotency_key")
                return runner.write(tool["name"], args, key, confirmed)
            return runner.call(tool["name"], args)

    # ---------- linking ----------

    def how_to_connect(self, user: dict, site: str) -> str:
        manifest = self.manifest(site)
        kind = manifest.get("auth", {}).get("type", "none")
        if kind == "none":
            return f"{site} needs no login."
        if kind == "session":
            return (f"Ask the user to open {manifest['base']} in Chrome, log in, and click 'Link session' "
                    f"in the connector extension. {install_extension(self.settings.public_url)}")
        token = secrets.token_urlsafe(24)
        with self.lock:
            now = time.time()
            self.links = {t: link for t, link in self.links.items() if link.expires > now}
            self.links[token] = Link(user, site, now + LINK_TTL)
        return (f"Give the user this link to link {site} (valid for ten minutes, works once): "
                f"{self.settings.public_url}/connect/{token}")

    def link(self, token: str) -> Link | None:
        with self.lock:
            link = self.links.get(token)
            return link if link and link.expires > time.time() and link.attempts < LINK_ATTEMPTS else None

    def connect(self, token: str, value) -> bool:
        """Links with what a person entered on the connect page. A link that worked no longer works after."""
        if not (link := self.link(token)):
            raise ConnectorError("This link no longer works.")
        with self.lock:
            link.attempts += 1
        with self.runner(link.user, link.site) as runner:
            try:
                ok = runner.auth.connect(value)
            except httpx2.HTTPError:
                raise ConnectorError(f"{link.site} cannot be reached right now. Try again later.") from None
        if ok:
            with self.lock:
                self.links.pop(token, None)
            log.info("connected user=%s site=%s", link.user["name"], link.site)
        return ok

    def connect_session(self, user: dict, url: str, session: dict) -> str:
        """A session from the user's browser, through the extension."""
        site = self.registry.find(host_of(url))
        if not site:
            raise ConnectorError("This site has no tools yet. Record something first.")
        if self.manifest(site).get("auth", {}).get("type") == "oauth":
            raise ConnectorError(f"Link {site} with the link from the connect tool.")
        if not SessionAuth(self.store(user, site)).connect(session):
            raise ConnectorError("No cookies found. Are you logged in on this site?")
        log.info("session user=%s site=%s", user["name"], site)
        return site

    # ---------- making tools ----------

    def _may_start(self, user: dict, limited: bool, now: float) -> None:
        """Busy or Limit, or nothing. Call with the lock held."""
        self.jobs = {i: j for i, j in self.jobs.items() if j.finished is None or j.finished > now - JOB_TTL}
        if any(j.user == user["id"] and j.finished is None for j in self.jobs.values()):
            raise Busy("A recording of yours is already running. Wait until it is done.")
        most = self.settings.recordings_per_month
        if limited and self.users.recordings_since(now - MONTH, user["id"]) >= most:
            raise Limit(f"You already made tools {most} times in the last 30 days, and more is not possible yet. "
                        "Ask us if you need more.")

    def ready(self, user: dict) -> None:
        """Busy or Limit before the server reads a recording: reading and parsing 25 MB costs time and memory."""
        limited = self.users.invited(user["id"])
        with self.lock:
            self._may_start(user, limited, time.time())

    def start(self, user: dict, recording: dict) -> str:
        """Starts making tools and returns an id at once. One recording at a time per user."""
        limited = self.users.invited(user["id"])
        with self.lock:
            now = time.time()
            self._may_start(user, limited, now)
            job_id = secrets.token_urlsafe(12)
            self.jobs[job_id] = job = Job(user["id"], host_of(recording["host"]))
            self.users.record(job_id, user["id"], job.host)
        threading.Thread(target=self._run, args=(job_id, job, user, recording), daemon=True).start()
        return job_id

    def job(self, user: dict, job_id: str) -> Job | None:
        with self.lock:
            job = self.jobs.get(job_id)
        return job if job and job.user == user["id"] else None

    def _run(self, job_id: str, job: Job, user: dict, recording: dict) -> None:
        try:
            job.result = self.extract(user, recording, lambda step: setattr(job, "step", step),
                                      lambda: self.users.asked(job_id))
        except ConnectorError as e:
            job.error = str(e)
        except anthropic.APIError as e:
            log.warning("extract user=%s model error=%s", user["name"], type(e).__name__)
            job.error = "The language model cannot be reached right now. Try again later."
        except Exception:
            log.exception("extract user=%s failed", user["name"])
            job.error = "Internal error while making tools."
        try:
            result = job.result or {}
            self.users.recorded(job_id, result.get("site"), result.get("new_site", False), result.get("added", []),
                                job.error)
        except Exception:
            log.exception("extract user=%s not logged", user["name"])
        finally:
            job.finished = time.time()

    def extract(self, user: dict, recording: dict, step: Callable[[str], None] = lambda step: None,
                asked: Callable[[], None] = lambda: None) -> dict:
        host = host_of(recording["host"])
        # By the base, not by the tab: a site on example.com with its API on api.example.com is one site.
        known = self.registry.find(pipeline.base(recording, host))
        site = known or self.registry.free_id(host)

        def ask(shown: dict) -> pipeline.Proposal:
            asked()
            step(f"Claude picks tools from {len(shown['endpoints'])} endpoints")
            return self.ask(shown)

        extracted = pipeline.extract(recording, host, self.registry.get(site), ask)
        if not known and extracted.manifest.get("runtime") == "browser" and self.users.invited(user["id"]):
            # The browser runtime runs the site's own JavaScript in Chrome on this server, at every call. A guest could
            # make any site of their own look bot-protected; only the team adds such a site.
            raise ConnectorError("This site has bot protection, so its tools run in the server's browser. Only the "
                                 "team can add such a site: ask them to record it.")
        step("Checking and saving tools")
        # A recording can be a crafted HAR, and a tool runs with the session of whoever calls it. With
        # REVIEW_GUEST_TOOLS=1 the server shares a guest's tools only after a review (python -m connector.registry).
        by = user["id"] if self.settings.review_guest_tools and self.users.invited(user["id"]) else None
        added = self.registry.add(site, extracted.manifest, by) if extracted.manifest["tools"] else []
        manifest = self.registry.get(site) or {}
        session = (manifest.get("auth", {}).get("type") in ("session", "password")
                   and SessionAuth(self.store(user, site)).connect(extracted.session))
        log.info("extracted user=%s site=%s added=%s rejected=%d", user["name"], site, added, len(extracted.rejected))
        return {"site": site, "new_site": not known and bool(added), "added": [f"{site}_{name}" for name in added],
                "rejected": extracted.rejected, "session": session, "shared": by is None}


class Busy(ConnectorError):
    pass


class Limit(ConnectorError):
    pass


def extension_id() -> str | None:
    """The extension's fixed id. It comes from the key in manifest.json, the way Chrome computes it too."""
    try:
        key = base64.b64decode(json.loads((EXTENSION / "manifest.json").read_text())["key"])
    except (OSError, KeyError, ValueError):
        return None
    return "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(key).hexdigest()[:32])


def extension_zip(public_url: str) -> bytes:
    """The extension as a zip, in a folder Chrome can load, with this server's address already filled in."""
    url = urlsplit(public_url)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(EXTENSION.iterdir()):
            if path.suffix not in EXTENSION_FILES:
                continue
            text = path.read_text()
            if path.name == "popup.js":
                text = re.sub(r'^const DEFAULT_SERVER = ".*";$', f"const DEFAULT_SERVER = {json.dumps(public_url)};",
                              text, count=1, flags=re.M)
            if path.name == "manifest.json":  # only this server's pages may give the extension a token
                manifest = json.loads(text)
                manifest["externally_connectable"] = {"matches": [f"{url.scheme}://{url.hostname}/*"]}
                text = json.dumps(manifest, indent=2, ensure_ascii=False)
            zipped.writestr(f"mcp-everything-extension/{path.name}", text)
    return buffer.getvalue()


# ---------- MCP ----------

def _schema(tool: dict) -> dict:
    schema = tool["input_schema"]
    if tool["effect"] != "write":
        return schema
    return {**schema, "properties": {**schema.get("properties", {}), "idempotency_key": KEY},
            "required": [*schema.get("required", []), "idempotency_key"]}


def _listed(site: str, tool: dict) -> types.Tool:
    write = tool["effect"] == "write"
    description = tool["description"] + (" Asks the user to confirm." if tool["human_only"] else "")
    return types.Tool(name=f"{site}_{tool['name']}", description=f"[{site}] {description}", input_schema=_schema(tool),
                      annotations=types.ToolAnnotations(read_only_hint=not write, destructive_hint=write,
                                                        idempotent_hint=True, open_world_hint=True))


META = [
    types.Tool(name="sites", description="The sites this server knows, their tools, and whether the user linked them.",
               input_schema={"type": "object", "properties": {}, "additionalProperties": False},
               annotations=types.ToolAnnotations(read_only_hint=True, open_world_hint=False)),
    types.Tool(name="connect", description="How the user links a site: an instruction or a one-time link.",
               input_schema={"type": "object", "properties": {"site": {"type": "string", "description": "Id from sites."}},
                             "required": ["site"], "additionalProperties": False},
               annotations=types.ToolAnnotations(read_only_hint=True, open_world_hint=False)),
]


def _result(data, error: bool = False) -> types.CallToolResult:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=1)
    if len(text) > MAX_RESULT:
        text = text[:MAX_RESULT] + "\n… (cut short; ask for less at once)"
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=error)


async def _confirm(ctx, params: types.CallToolRequestParams, message: str, seal: str) -> bool | types.InputRequiredResult:
    """Lets the user confirm themselves. The model cannot do this: the answer comes from the client.

    `seal` binds a yes to exactly this call: a retry with other input asks again."""
    if not ctx.session.check_client_capability(types.ClientCapabilities(elicitation=types.ElicitationCapability())):
        raise ConnectorError("The user confirms this action themselves, and this client cannot ask for that. "
                             "Nothing was sent. The user can do it on the site themselves.")
    if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
        answer = (params.input_responses or {}).get("confirm")
        if answer is None or not hmac.compare_digest(params.request_state or "", seal):
            return types.InputRequiredResult(request_state=seal, input_requests={"confirm": types.ElicitRequest(
                params=types.ElicitRequestFormParams(message=message, requested_schema=CONFIRM))})
    else:
        try:
            answer = await ctx.session.elicit_form(message, CONFIRM, ctx.request_id)
        except MCPError:
            raise ConnectorError("The confirmation did not arrive. Nothing was sent.") from None
    return isinstance(answer, types.ElicitResult) and answer.action == "accept" and (answer.content or {}).get("confirm") is True


def mcp_server(connector: Connector) -> Server:
    async def list_tools(ctx, params) -> types.ListToolsResult:
        # ponytail: every tool of every site. With hundreds of sites: only linked sites, plus search through `sites`.
        user = _api_user(ctx.request)
        tools = [_listed(site, tool) for site in connector.registry.ids()
                 for tool in visible(connector.registry.get(site), user["id"])["tools"]]
        return types.ListToolsResult(tools=META + tools)

    async def call_tool(ctx, params: types.CallToolRequestParams):
        user, name, args = _api_user(ctx.request), params.name, dict(params.arguments or {})
        started = time.monotonic()
        try:
            if name == "sites":
                return _result(await anyio.to_thread.run_sync(connector.sites, user))
            if name == "connect":
                return _result(await anyio.to_thread.run_sync(connector.how_to_connect, user, str(args.get("site", ""))))
            site, tool = connector.tool(user, name)
            if error := next(jsonschema.Draft202012Validator(_schema(tool)).iter_errors(args), None):
                raise ConnectorError(f"Invalid input: {error.message}")
            confirmed = False
            if tool["effect"] == "write" and tool["human_only"]:
                shown = json.dumps({k: v for k, v in args.items() if k != "idempotency_key"}, ensure_ascii=False, indent=1)
                seal = hmac.new(connector.seal_key, json.dumps([user["id"], name, args], sort_keys=True).encode(),
                                hashlib.sha256).hexdigest()
                answer = await _confirm(ctx, params, f"{site}: {tool['description']}\n\n{shown}", seal)
                if isinstance(answer, types.InputRequiredResult):
                    return answer
                if not answer:
                    return _result("Not done: the user did not confirm.", error=True)
                confirmed = True
            data = await anyio.to_thread.run_sync(connector.call, user, site, tool, args, confirmed)
            log.info("call user=%s tool=%s ok %.1fs", user["name"], name, time.monotonic() - started)
            return _result(data)
        except AuthExpired as e:
            return _result(f"{e}. Link it with the connect tool (site: {name.partition('_')[0]}).", error=True)
        except ConnectorError as e:
            log.info("call user=%s tool=%s error=%s", user["name"], name, type(e).__name__)
            return _result(str(e), error=True)
        except Exception:
            log.exception("call user=%s tool=%s failed", user["name"], name)
            return _result("Internal error in the connector. Nothing was retried.", error=True)

    return Server("mcp-everything", version="0.4.0",
                  instructions=f"{INSTRUCTIONS} {install_extension(connector.settings.public_url)}",
                  on_list_tools=list_tools, on_call_tool=call_tool)


# ---------- HTTP ----------

class Tokens:
    """Bearer tokens for MCP and the extension: an API token from `connector.users`, or an access token from OAuth."""

    def __init__(self, users: Users, provider: oauth.OAuth):
        self.users, self.provider = users, provider

    async def verify_token(self, token: str) -> AccessToken | None:
        if user := self.users.by_token(token):
            return oauth.access(token, "api", user["id"], user["name"], [])
        return await self.provider.load_access_token(token)


def _api_user(request: Request) -> dict:
    if not request.user.is_authenticated:
        raise HTTPException(401, "Give your token as Authorization: Bearer <token>.")
    token: AccessToken = request.user.access_token
    return {"id": token.subject, "name": token.claims["name"]}


async def _json(request: Request, limit: int) -> dict:
    if int(request.headers.get("content-length") or 0) > limit:
        raise HTTPException(413, "Too large.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(413, "Too large.")
    try:  # in a thread: a big body takes a second to parse, and the event loop serves everyone
        data = await anyio.to_thread.run_sync(json.loads, bytes(body))
    except ValueError:
        raise HTTPException(400, "Not valid JSON.") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "Expected an object.")
    return data


RECORDING = {"type": "object", "required": ["host", "log"], "properties": {
    "host": {"type": "string", "minLength": 3, "maxLength": 253},
    "log": {"type": "object", "required": ["entries"], "properties": {"entries": {
        "type": "array", "maxItems": 5000, "items": {"type": "object", "required": ["request", "response"], "properties": {
            "request": {"type": "object", "required": ["url", "method"],
                        "properties": {"url": {"type": "string"}, "method": {"type": "string"}}},
            "response": {"type": "object", "required": ["status", "content"]}}}}}}}}
SESSION_BODY = {"type": "object", "required": ["url", "cookies"], "properties": {
    "url": {"type": "string", "maxLength": 2000},
    "cookies": {"type": "object", "maxProperties": 200, "additionalProperties": {"type": "string", "maxLength": 8192}}}}


def _valid(data: dict, schema: dict) -> None:
    if error := next(jsonschema.Draft202012Validator(schema).iter_errors(data), None):
        raise HTTPException(400, f"Invalid: {error.message[:200]}")


def app(settings: Settings, connector: Connector | None = None) -> Starlette:
    connector = connector or Connector(settings)
    public = urlsplit(settings.public_url)
    origin = f"{public.scheme}://{public.netloc}"
    extension = extension_id()
    provider = oauth.OAuth(connector.users, settings.public_url, settings.key)
    # AuthSettings only for the addresses: that way the issuer gets no trailing /, just as in the SDK itself.
    urls = AuthSettings(issuer_url=settings.public_url, resource_server_url=f"{settings.public_url}/mcp",
                        validate_token_resource=False)
    manager = StreamableHTTPSessionManager(
        app=mcp_server(connector),
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public.netloc, "127.0.0.1:*", "localhost:*"],
            allowed_origins=[origin, "http://127.0.0.1:*", "http://localhost:*"]))

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def sites(request: Request) -> Response:
        return JSONResponse(await anyio.to_thread.run_sync(connector.sites, _api_user(request)))

    async def recordings(request: Request) -> Response:
        user = _api_user(request)
        try:
            await anyio.to_thread.run_sync(connector.ready, user)
        except (Busy, Limit) as e:
            return JSONResponse({"error": str(e)}, 429)
        recording = await _json(request, MAX_RECORDING)
        await anyio.to_thread.run_sync(_valid, recording, RECORDING)
        try:
            job = await anyio.to_thread.run_sync(connector.start, user, recording)
        except (Busy, Limit) as e:
            return JSONResponse({"error": str(e)}, 429)
        return JSONResponse({"job": job, **connector.job(user, job).view()}, 202)

    async def recording(request: Request) -> Response:
        if not (job := connector.job(_api_user(request), request.path_params["job"])):
            return JSONResponse({"error": "The server does not know this recording (anymore). Record again."}, 404)
        return JSONResponse(job.view())

    async def sessions(request: Request) -> Response:
        user = _api_user(request)
        body = await _json(request, 1_000_000)
        _valid(body, SESSION_BODY)
        try:
            site = await anyio.to_thread.run_sync(connector.connect_session, user, body["url"], {"cookies": body["cookies"]})
        except ConnectorError as e:
            return JSONResponse({"error": str(e)}, 422)
        return JSONResponse({"site": site})

    async def connect_page(request: Request) -> Response:
        token = request.path_params["token"]
        if not (link := connector.link(token)):
            return pages.expired()
        auth = connector.manifest(link.site).get("auth", {})
        if request.method == "GET":
            return _connect_form(link.site, auth, account=link.user["name"])
        form = await request.form(max_files=0, max_fields=20, max_part_size=10_000)
        if auth["type"] == "password":
            value = {f: str(form.get(f, "")) for f in auth["fields"]}
        else:
            text = str(form.get("code", "")).strip()
            value = parse_qs(urlsplit(text).query).get("code", [text])[0]
        try:
            ok = await anyio.to_thread.run_sync(connector.connect, token, value)
        except ConnectorError as e:
            ok, reason = False, str(e)
        else:
            reason = "Logging in failed. Check your details and try again."
        return pages.done(link.site) if ok else _connect_form(link.site, auth, reason, link.user["name"])

    # scrypt costs 16 MiB and tens of milliseconds, and anyone can ask for it (a login, a sign-up): a few at a time, so a
    # flood of wrong passwords does not take the memory and the threads that the tools and the database need.
    scrypt = anyio.CapacityLimiter(SCRYPT_AT_ONCE)

    async def make_account(code: str, name: str, password: str) -> tuple[str, dict]:
        """An account from an invite: its token, and the user. A SignupError says what is wrong."""
        token = await anyio.to_thread.run_sync(connector.users.redeem, code, name, settings.max_users, password,
                                               limiter=scrypt)
        user = await anyio.to_thread.run_sync(connector.users.by_token, token)
        log.info("signup user=%s", user["name"])
        return token, user

    async def signup(request: Request) -> Response:
        code, users, most = request.path_params["code"], connector.users, settings.max_users
        if request.method == "GET":
            open_ = await anyio.to_thread.run_sync(users.open_invite, code, most)
            return pages.signup(extension) if open_ else pages.closed()
        if not _same_origin(request, origin):  # a form from another site: it could hand the extension its own token
            open_ = await anyio.to_thread.run_sync(users.open_invite, code, most)
            return pages.signup(extension, "Make your account from this page.") if open_ else pages.closed()
        form = await request.form(max_files=0, max_fields=5, max_part_size=1_000)
        try:
            token, user = await make_account(code, str(form.get("name", "")), str(form.get("password", "")))
        except SignupError as e:
            open_ = await anyio.to_thread.run_sync(users.open_invite, code, most)
            return pages.signup(extension, str(e)) if open_ else pages.closed()
        response = pages.welcome(user["name"], token, settings.public_url, extension)
        _remember(response, provider, user["id"], public.scheme == "https")
        return response

    failures: dict[str, list[float]] = {}  # name: when a password was wrong. Only the event loop touches it.

    async def login(request: Request) -> Response:
        request_id = request.path_params["request"]
        known = await anyio.to_thread.run_sync(provider.from_cookie, request.cookies.get(oauth.COOKIE))
        name = known["name"] if known else None
        if not (pending := provider.pending(request_id)):
            used = provider.used(request_id)
            return pages.login_done(used, name, extension) if used else pages.login_expired(name)
        form_to = f"'self' {pending.source}".strip()

        def show(error: str | None = None, **signup: str) -> Response:
            return pages.login(pending.client_name, pending.back_to, extension, form_to, name, error, **signup)

        def tried(outcome: str, who: str) -> None:
            # Behind Caddy the client is Caddy: it puts the real address in X-Forwarded-For, over what the browser sent.
            ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "-").split(",")[0]
            logins.info("%s name=%r ip=%s agent=%r", outcome, who[:64], ip.strip(),
                        request.headers.get("user-agent", "")[:120])

        if request.method == "GET":
            return show()
        if not _same_origin(request, origin):  # a form from another site: do not log in, and make no account
            return show("Log in from this page.")
        form = await request.form(max_files=0, max_fields=5, max_part_size=2_000)
        if form.get("action") == "deny":
            return RedirectResponse(redirect, 303) if (redirect := provider.deny(request_id)) else pages.login_expired(name)
        if form.get("action") == "signup":
            # New here: an account from the invite, and the request stays open while the extension gets installed.
            # The cookie knows the new user, so "Continue" below (and a retry from Claude) is one click.
            invite, new = str(form.get("invite", "")).strip(), str(form.get("name", "")).strip().lower()
            try:  # the bare code, or the whole invite link: then the code is the last part of the path
                token, user = await make_account(urlsplit(invite).path.rstrip("/").rpartition("/")[2], new,
                                                 str(form.get("password", "")))
            except SignupError as e:
                return show(str(e), invite=invite, new=new)
            response = pages.login_welcome(pending.client_name, pending.back_to, user["name"], token, extension, form_to)
            _remember(response, provider, user["id"], public.scheme == "https")
            return response
        token, password = str(form.get("token", "")).strip(), str(form.get("password", ""))
        who = str(form.get("user", "")).strip().lower()
        if token:
            user = await anyio.to_thread.run_sync(connector.users.by_token, token)
        elif password:
            # Guessing: at most TRIES wrong passwords per name per ten minutes. ponytail: someone who knows a name can
            # lock that person out of the password for ten minutes (the cookie, the extension and the token still work).
            now = time.time()
            if len(failures) > 1000:  # names without a recent wrong password go: many names do not fill the memory
                for old in [n for n, times in failures.items() if not times or times[-1] < now - 600]:
                    del failures[old]
            failures[who] = [t for t in failures.get(who, []) if t > now - 600]
            if len(failures[who]) >= TRIES:
                tried("locked", who)
                return show("Too many wrong passwords for this name. Wait ten minutes, or log in with your token.",
                            who=who)
            if not (user := await anyio.to_thread.run_sync(connector.users.by_password, who, password, limiter=scrypt)):
                failures[who].append(now)
            else:  # only the right password for this name clears its count, not a login with a token or cookie
                failures.pop(who, None)
        else:
            user = known
        if not user:
            if token or password:
                tried("wrong token" if token else "wrong password", who)
            return show("The server does not know that token." if token else
                        "That name and password do not match." if password else
                        "Log in with your name and password, or with the extension.", who=who)
        tried("ok, token" if token else "ok, password" if password else "ok, cookie", user["name"])
        if token and form.get("action") == "allow":
            # Pasted, not from the extension (that sends no action): this browser may not have the extension yet. The
            # step of a new account offers it, with this token. Its "Continue" logs in with the cookie.
            response = pages.login_welcome(pending.client_name, pending.back_to, user["name"], token, extension, form_to,
                                           new=False)
            _remember(response, provider, user["id"], public.scheme == "https")
            return response
        if not (redirect := provider.approve(request_id, user["id"])):
            return pages.login_expired(name)
        log.info("login user=%s client=%s", user["name"], pending.client.client_id)
        response = RedirectResponse(redirect, 303)
        _remember(response, provider, user["id"], public.scheme == "https")
        return response

    async def download(request: Request) -> Response:
        return Response(await anyio.to_thread.run_sync(extension_zip, settings.public_url), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="mcp-everything-extension.zip"'})

    video = settings.data / "about.mp4"

    async def about(request: Request) -> Response:
        if not video.is_file():
            raise HTTPException(404, "No video yet.")
        return pages.about() if request.url.path == "/about" else FileResponse(video, media_type="video/mp4")

    async def http_error(request: Request, exc: HTTPException) -> Response:
        return JSONResponse({"error": exc.detail}, exc.status_code)

    resource = create_protected_resource_routes(resource_url=urls.resource_server_url,
                                                authorization_servers=[urls.issuer_url], scopes_supported=oauth.SCOPES,
                                                resource_name="MCP Everything")
    return Starlette(
        routes=[Route("/mcp", RequireAuthMiddleware(StreamableHTTPASGIApp(manager), [],
                                                    build_resource_metadata_url(urls.resource_server_url))),
                *create_auth_routes(provider, urls.issuer_url,
                                    client_registration_options=ClientRegistrationOptions(
                                        enabled=True, default_scopes=oauth.SCOPES),
                                    revocation_options=RevocationOptions(enabled=True)),
                *resource,
                # Some clients look for the metadata without /mcp at the end.
                Route("/.well-known/oauth-protected-resource", resource[0].endpoint, methods=resource[0].methods),
                Route("/login/{request}", login, methods=["GET", "POST"]),
                Route("/healthz", healthz),
                # The extension talks to the server without permission for this domain: CORS, only for its fixed id.
                # Only on /api: the SDK's OAuth routes have their own CORS.
                Mount("/api", routes=[Route("/sites", sites),
                                      Route("/recordings", recordings, methods=["POST"]),
                                      Route("/recordings/{job}", recording),
                                      Route("/sessions", sessions, methods=["POST"])],
                      middleware=[Middleware(CORSMiddleware, allow_origins=[f"chrome-extension://{extension}"],
                                             allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])]
                      if extension else None),
                Route("/connect/{token}", connect_page, methods=["GET", "POST"]),
                Route("/signup/{code}", signup, methods=["GET", "POST"]),
                Route("/extension.zip", download),
                Route("/about", about),
                Route("/about.mp4", about)],
        middleware=[Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(Tokens(connector.users, provider)))],
        exception_handlers={HTTPException: http_error},
        lifespan=lambda _: manager.run())


def _same_origin(request: Request, origin: str) -> bool:
    """Did this form come from our own page? A browser says so with Sec-Fetch-Site, an older one with Origin."""
    return request.headers.get("sec-fetch-site") == "same-origin" or request.headers.get("origin") == origin


def _remember(response: Response, provider: oauth.OAuth, user_id: str, secure: bool) -> None:
    """From now on this browser knows the user on /login: logging in next time is one click."""
    response.set_cookie(oauth.COOKIE, provider.cookie(user_id), max_age=oauth.COOKIE_TTL, path="/login",
                        secure=secure, httponly=True, samesite="lax")


def _connect_form(site: str, auth: dict, error: str | None = None, account: str = "") -> Response:
    if auth["type"] == "password":
        return pages.password(site, auth["fields"], error, account)
    redirect = parse_qs(urlsplit(auth["authorize_url"]).query).get("redirect_uri", [""])[0]
    return pages.oauth(site, auth["authorize_url"], redirect, extension_id(), error, account)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # logs every URL, with search text and sometimes a session id
    settings = Settings.from_env()
    # Logins also go to a file in the data folder: the log of the container is gone with every deploy.
    to_file = logging.handlers.RotatingFileHandler(settings.data / "logins.log", maxBytes=5_000_000, backupCount=1,
                                                   delay=True)
    to_file.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logins.addHandler(to_file)
    # No access log: a connect link or invite is in the path.
    uvicorn.run(app(settings), host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")),
                access_log=False)


if __name__ == "__main__":
    main()
