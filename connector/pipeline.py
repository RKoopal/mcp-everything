"""From recording to tools: `extract(har, host, known)`.

As little as possible goes to the language model; code does what can be decided.

1. Group endpoints, with the same key as `har.summarize`.
2. Take the session out of the recording: cookies and secret headers. It goes to the user's
   vault, never to the model and never to the registry.
3. Decide the fixed headers and the runtime. Bot-protection cookies mean: run in the browser.
4. One call to Claude. The model sees no secrets and writes no requests: it picks which
   seen endpoints are a tool, names them and points out which values are inputs.
5. Compile and check. The request comes from the recording; only the places pointed out become
   inputs. What does not fit is dropped with a reason. `manifest.check` checks the rest.
"""
from __future__ import annotations

import copy
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

import anthropic
from pydantic import BaseModel, Field

from . import har
from .auth import BOT_COOKIES, cookie_pairs
from .errors import ConnectorError
from .manifest import NAME, PLACEHOLDER, ManifestError, check_tool

MODEL = "claude-opus-5-5"
MAX_ENDPOINTS = 60
MAX_TEXT = 300
SECRET_NAME = re.compile(r"pass|token|secret|auth|session|csrf|xsrf|signature|api[-_]?key|otp|cookie", re.I)
EMAIL = re.compile(r"[^@\s/]+@[^@\s/]+\.[a-z]{2,}", re.I)
# Data of one person. It does not belong in a shared manifest: every other user would send it along.
PERSONAL_NAME = re.compile(r"^((first|last|full|given|family|sur|display|user|customer|contact)[-_]?)?name$|phone|mobile|"
                           r"telephone|street|address|post[-_]?code|postal|(^|[-_])zip|birth|iban|customer|member|"
                           r"person|(user|account|profile|client)[-_]?id$", re.I)
PERSONAL_VALUE = re.compile(r"\d{4} ?[A-Z]{2}|[A-Z]{2}\d{2}[A-Z]{4}\d{7,}")  # postcode, IBAN
PHONE = re.compile(r"(\+|0)[\d ()-]+")  # phone: with at least nine digits, so a date does not match
# Values of the site itself, not of the user: a persisted GraphQL query has a hash.
SITE_VALUES = {"sha256Hash", "operationName", "query"}
# Headers the browser sets itself. They do not belong in a shared manifest: they are of one browser.
BROWSER_HEADERS = {"accept-encoding", "accept-language", "cache-control", "connection", "content-length",
                   "content-type", "cookie", "dnt", "host", "origin", "pragma", "priority", "referer", "te",
                   "upgrade-insecure-requests", "user-agent", "x-client-data"}
GRAPHQL_QUERY = re.compile(r"\s*(query\b|\{)")
# A document can have more operations; operationName picks one. A mutation anywhere at the top level counts as a write.
# GraphQL reads commas, comments and a byte-order mark as spaces: "query A{a},mutation B{x}" is a write too.
GRAPHQL_STRINGS = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"')
GRAPHQL_IGNORED = re.compile(r"#[^\n\r]*|[,\ufeff]")
GRAPHQL_TOKENS = re.compile(r"[A-Za-z_]\w*|[{}()]")
LOCATIONS = ("path", "query", "body", "form")

SYSTEM = """You get the endpoints from a recording of a website: requests the site itself made while \
a person clicked. Make tools from them for an AI agent that works on behalf of this user.

- Only pick endpoints a user would want to call themselves: search, lists, details, adding or \
changing something. Skip tracking, analytics, logging, feature flags, configuration and bot detection.
- Every tool belongs to exactly one endpoint (index).
- Every value that differs per call (search text, ids, amounts, dates, pages) becomes a \
parameter. What is the same for everyone (a GraphQL query, a version number) stays fixed.
- <secret>, <token>, <email> and <personal> are values left out. If such a value is an input, like an id, make it \
a parameter. Otherwise the tool cannot work: leave it out.
- location is path:<index in segments>, query:<name>, body:<JSON pointer, like /variables/id> or \
form:<name>.
- effect: read if the request changes nothing, otherwise write. reversible: the name of the tool that undoes \
this write, only if that tool is there too.
- result_path: only if the shape of the response is known. The path (a.b.c) to the useful part.
- verify_path: for a write, the path in the response that is only filled on success, like the id \
of what you made.
- name: English, snake_case, unique, like search_products. description: English, one or two sentences \
for an agent: what the tool does and what it returns.

Better a few tools that are right than many that guess."""


class Param(BaseModel):
    name: str = Field(description="snake_case, unique within the tool")
    type: Literal["string", "integer", "number", "boolean"]
    description: str
    required: bool
    location: str = Field(description="path:<index>, query:<name>, body:<JSON pointer> or form:<name>")


class ToolProposal(BaseModel):
    endpoint: int = Field(description="index of the endpoint")
    name: str
    description: str
    effect: Literal["read", "write"]
    params: list[Param]
    reversible: str | None = None
    result_path: str | None = None
    verify_path: str | None = None


class Proposal(BaseModel):
    tools: list[ToolProposal]


@dataclass
class Extracted:
    manifest: dict                                 # only the new tools; no values of the user
    session: dict                                  # {"cookies": ..., "headers": ...} for the vault
    rejected: list[str] = field(default_factory=list)


class Rejected(ValueError):
    pass


# ---------- recognizing secrets ----------

def _entropy(text: str) -> float:
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def sensitive(value, name: str = "", secrets: frozenset = frozenset()) -> str | None:
    """What a value is if it must not go to the model or the registry: secret, token, email or personal."""
    if isinstance(value, int | float) and not isinstance(value, bool) and PERSONAL_NAME.search(name):
        return "<personal>"  # like {"customerId": 884213}
    if not isinstance(value, str) or not value or name in SITE_VALUES:
        return None
    if SECRET_NAME.search(name) or value in secrets:
        return "<secret>"
    if EMAIL.search(value):
        return "<email>"
    if PERSONAL_NAME.search(name) or PERSONAL_VALUE.fullmatch(value.strip()) or \
            (PHONE.fullmatch(value.strip()) and sum(c.isdigit() for c in value) >= 9):
        return "<personal>"
    looks_random = len(value) >= 16 and not re.search(r"\s", value) and _entropy(value) >= 3.0
    if looks_random and re.search(r"\d", value) and re.search(r"[A-Za-z]", value):
        return "<token>"
    return None


def redact(value, name: str = "", secrets: frozenset = frozenset()):
    if isinstance(value, dict):
        return {k: redact(v, k, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, name, secrets) for v in value[:3]]
    if hidden := sensitive(value, name, secrets):
        return hidden
    return value[:MAX_TEXT] + "…" if isinstance(value, str) and len(value) > MAX_TEXT else value


def _leaves(value, name: str = ""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _leaves(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from _leaves(v, name)
    else:
        yield name, value


# ---------- deciding what can be decided ----------

def _headers(entry: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in entry["request"].get("headers", []) if not h["name"].startswith(":")}


# Analytics and consent cookies: from the browser, not a login. Without them a public site does not look
# like a site with a session, and they do not go into the vault.
TRACKING_COOKIES = re.compile(r"_ga\w*|_gid|_gat\w*|_gcl_\w+|_fbp|_fbc|_hj\w+|_clck|_clsk|_uet\w+|_pin_unauth|_tt_\w+|"
                              r"_ttp|ajs_\w+|amplitude\w*|mp_\w+|_pk_\w+|optanon\w*|cookieconsent\w*|euconsent\w*|"
                              r"cookie_?consent\w*|consent\w*|cookiebot\w*|__utm\w", re.I)


def session_of(entries: list[dict]) -> dict:
    """The session as the last requests sent it. Bot-protection and analytics cookies stay out."""
    cookies, headers = {}, {}
    for entry in entries:
        sent = _headers(entry)
        cookies |= cookie_pairs(sent.get("cookie", ""))
        # Also a header of this one user, such as x-customer-id: it belongs in their vault, not in the manifest.
        # No sec-*: Chrome sets those itself (sec-ch-ua-mobile), and then every site would seem to have a session.
        headers |= {k: v for k, v in sent.items() if (SECRET_NAME.search(k) or PERSONAL_NAME.search(k))
                    and k not in BROWSER_HEADERS and not k.startswith("sec-")}
    return {"cookies": {k: v for k, v in cookies.items() if not BOT_COOKIES.fullmatch(k) and not TRACKING_COOKIES.fullmatch(k)},
            "headers": headers}


def constant_headers(entries: list[dict], secrets: frozenset = frozenset()) -> dict:
    """Headers every request sent with the same value, such as Accept-Version. No secrets: they are shared."""
    sets = [{(k, v) for k, v in _headers(e).items() if not k.startswith("sec-") and k not in BROWSER_HEADERS
             and not sensitive(v, k, secrets)} for e in entries]
    return dict(sorted(set.intersection(*sets))) if sets else {}


def protected(entries: list[dict]) -> bool:
    return any(BOT_COOKIES.fullmatch(name) for e in entries for name in cookie_pairs(_headers(e).get("cookie", "")))


def _body(entry: dict):
    text = entry["request"].get("postData", {}).get("text")
    try:
        return "json", json.loads(text) if text else None
    except ValueError:
        return "form", dict(parse_qsl(text or "", keep_blank_values=True)) or None


def _is_query(body) -> bool:
    items = body if isinstance(body, list) else [body]
    documents = [_graphql(i["query"]) if isinstance(i, dict) and isinstance(i.get("query"), str) else None for i in items]
    return bool(items) and all(d is not None and GRAPHQL_QUERY.match(d) and not _top_level({"mutation", "subscription"}, d)
                               for d in documents)


def _graphql(document: str) -> str:
    """The document without strings, comments, commas and byte-order marks: what is left are the tokens."""
    return GRAPHQL_IGNORED.sub(" ", GRAPHQL_STRINGS.sub('""', document))


def _top_level(words: set[str], document: str) -> bool:
    """Is one of `words` a token outside every {} and (), where an operation starts? A field named `subscription`
    inside a query is not."""
    depth = 0
    for token in GRAPHQL_TOKENS.findall(document):
        if token in ("{", "("):
            depth += 1
        elif token in ("}", ")"):
            depth -= 1
        elif depth == 0 and token in words:
            return True
    return False


def _pattern(path: str) -> re.Pattern:
    return re.compile(re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(path)) + "$")


def _known(endpoint: dict, tools: list[dict]) -> bool:
    """Does the registry already have this endpoint as a tool? Then the model does not need to look at it."""
    url = urlsplit(endpoint["example"]["request"]["url"])
    for tool in tools:
        request = tool["request"]
        if request["method"] != endpoint["method"] or not _pattern(request["path"]).match(url.path):
            continue
        if not endpoint["operation"] or endpoint["operation"] in json.dumps(request.get("json", "")):
            return True
    return False


def endpoints(entries: list[dict]) -> list[dict]:
    """The API requests per endpoint, in order of first use, with one example."""
    summary = har.summarize({"log": {"entries": entries}})
    examples: dict[tuple, dict] = {}
    for entry in entries:
        if har.kind(entry) == "api":
            body = har.request_body(entry["request"].get("postData", {}).get("text"))
            examples.setdefault(har.key(entry, body), entry)
    return [{**g, "example": examples[(g["method"], g["host"], g["path"], g["operation"])]} for g in summary["api"]]


def _fixed(name: str, value):
    """The fixed values of a request, also inside a JSON string such as ?variables={"email": …}: they go to every user."""
    if isinstance(value, str) and PLACEHOLDER.fullmatch(value):
        return  # an input, not a value
    if isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None  # a GraphQL query starts with { too
        if isinstance(parsed, dict | list):
            for inner_name, inner in _leaves(parsed, name):
                yield from _fixed(inner_name, inner)
            return
    yield name, value


# ---------- the model ----------

def view(found: list[dict], host: str, secrets: frozenset) -> dict:
    """What the model sees: shapes and examples, without secrets."""
    shown = []
    for index, endpoint in enumerate(found):
        url = urlsplit(endpoint["example"]["request"]["url"])
        sort, body = _body(endpoint["example"])
        shown.append({
            "index": index, "method": endpoint["method"], "segments": [redact(s, "", secrets) for s in url.path.split("/")[1:]],
            "operation": endpoint["operation"], "query": redact(dict(parse_qsl(url.query, keep_blank_values=True)), "", secrets),
            sort if body is not None else "body": redact(body, "", secrets), "response": endpoint["response"],
            "count": endpoint["count"], "statuses": endpoint["statuses"], "pages": endpoint["pages"][:5],
        })
    return {"site": host, "endpoints": shown}


def ask_claude(shown: dict, client: anthropic.Anthropic | None = None, model: str = MODEL) -> Proposal:
    """The only call to a language model in the pipeline. On a refusal the API itself falls back to another model."""
    client = client or anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(shown, ensure_ascii=False)}],
        output_format=Proposal,
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise ConnectorError(f"The model returned no tools ({response.stop_reason}).")
    return response.parsed_output


# ---------- compiling ----------

def _set(document, pointer: str, value):
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]
    if not pointer.startswith("/") or not parts:
        raise Rejected(f"invalid JSON pointer {pointer}")
    node = document
    try:
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        last = int(parts[-1]) if isinstance(node, list) else parts[-1]
        node[last]  # must exist: the model may not make up fields
        node[last] = value
    except (KeyError, IndexError, ValueError, TypeError):
        raise Rejected(f"{pointer} is not in the request") from None


def _in_shape(shape, path: str) -> bool:
    for part in filter(None, path.split(".")):
        if isinstance(shape, list):
            shape = shape[0] if shape and part.isdigit() else None
        shape = shape.get(part) if isinstance(shape, dict) else None
        if shape is None:
            return False
    return True


def compile_tool(proposal: ToolProposal, endpoint: dict, writes: set[str], secrets: frozenset, today: str) -> dict:
    example = endpoint["example"]
    url = urlsplit(example["request"]["url"])
    segments = [s.split(";")[0] for s in url.path.split("/")[1:]]
    query = dict(parse_qsl(url.query, keep_blank_values=True))
    sort, body = _body(example)
    body = copy.deepcopy(body)
    properties, required = {}, []
    for param in proposal.params:
        kind, _, at = param.location.partition(":")
        if not NAME.fullmatch(param.name) or param.name in properties or kind not in LOCATIONS:
            raise Rejected(f"parameter {param.name} at {param.location} is not valid")
        slot = "{" + param.name + "}"
        if kind == "path":
            if not at.isdigit() or int(at) >= len(segments) or not segments[int(at)]:
                raise Rejected(f"path has no segment {at}")
            segments[int(at)] = slot
        elif kind == "query" and at in query:
            query[at] = slot
        elif kind == "body" and sort == "json" and body is not None:
            _set(body, at, slot)
        elif kind == "form" and sort == "form" and body and at in body:
            body[at] = slot
        else:
            raise Rejected(f"{param.location} is not in the request")
        properties[param.name] = {"type": param.type, "description": param.description}
        if param.required or kind == "path":
            required.append(param.name)

    for value in segments:
        if har.ID.fullmatch(value) or sensitive(value, "", secrets):
            raise Rejected(f"fixed value '{value[:12]}' in the path looks like an id or secret: make it an input")
    for name, value in [pair for n, v in [*query.items(), *_leaves(body)] for pair in _fixed(n, v)]:
        if sensitive(value, name, secrets):
            raise Rejected(f"fixed value in '{name}' looks personal or secret")

    method = endpoint["method"]
    safe = method == "GET" or (sort == "json" and _is_query(body))
    effect = "read" if safe and proposal.effect == "read" else "write"
    request = {"method": method, "path": "/" + "/".join(segments)}
    if query:
        request["query"] = query
    if body is not None:
        request["json" if sort == "json" else "form"] = body
    seen = endpoint["response"] is not None
    tool = {
        "name": proposal.name, "description": proposal.description, "effect": effect, "human_only": False,
        "input_schema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        "output_schema": endpoint["response"] or {}, "request": request, "depends_on": [],
        "verifier": "The response matches what the site shows.", "evidence": "traffic",
        "notes": f"Extracted from a recording on {today}. "
                 + ("Response seen." if seen else "Response not seen: the shape is unknown."),
    }
    if proposal.result_path and seen and _in_shape(endpoint["response"], proposal.result_path):
        tool["response"] = {"path": proposal.result_path}
    if effect == "write":
        reversible = proposal.reversible if proposal.reversible in writes - {proposal.name} else None
        tool["reversible"] = reversible
        tool["human_only"] = reversible is None or not proposal.verify_path
        if proposal.verify_path:
            tool["verifier"] = {"path": proposal.verify_path}
    try:
        check_tool(tool)
    except ManifestError as e:
        raise Rejected(str(e)) from None
    return tool


# ---------- all together ----------

def _traffic(recording: dict, host: str) -> tuple[list[dict], list[dict]]:
    """The requests to `host` and its subdomains, and the API endpoints in them."""
    entries = [e for e in recording["log"]["entries"]
               if (name := urlsplit(e["request"]["url"]).hostname or "") == host or name.endswith("." + host)]
    if not (found := endpoints(entries)):
        raise ConnectorError("No API requests in this recording. Do on the site what your agent should do, then record again.")
    return entries, found


def _origin(found: list[dict]) -> str:
    netloc = Counter(e["host"] for e in found).most_common(1)[0][0]
    example = next(e for e in found if e["host"] == netloc)["example"]
    return f"{urlsplit(example['request']['url']).scheme}://{netloc}"


def base(recording: dict, host: str) -> str:
    """The origin most API requests went to. The registry knows the site by it."""
    return _origin(_traffic(recording, host)[1])


def extract(recording: dict, host: str, known: dict | None = None, ask=ask_claude) -> Extracted:
    """`known` is the manifest the registry already has for this site; its base and login stay."""
    entries, found = _traffic(recording, host)
    origin = known["base"] if known else _origin(found)
    netloc = urlsplit(origin).netloc
    on_base = [e for e in entries if urlsplit(e["request"]["url"]).netloc == netloc]
    api = [e for e in on_base if har.kind(e) == "api"]
    session = session_of(api)
    secrets = frozenset(session["cookies"].values()) | frozenset(session["headers"].values())
    rejected = [f"{e['method']} {e['path']}: ander domein" for e in found if e["host"] != netloc]
    candidates = [e for e in found if e["host"] == netloc and not _known(e, known["tools"] if known else [])]
    if known:
        manifest = {k: v for k, v in known.items() if k != "tools"}
    else:
        manifest = {"site": urlsplit(origin).hostname.removeprefix("www."), "base": origin,
                    "runtime": "browser" if protected(api) else "http",
                    "auth": {"type": "session" if session["cookies"] or session["headers"] else "none"},
                    "headers": constant_headers(api, secrets)}
    if not candidates:
        return Extracted({**manifest, "tools": []}, session, rejected)

    candidates = candidates[:MAX_ENDPOINTS]
    proposal = ask(view(candidates, host, secrets))
    writes = {p.name for p in proposal.tools if p.effect == "write"}
    tools, today = [], date.today().isoformat()
    for p in proposal.tools:
        try:
            if not 0 <= p.endpoint < len(candidates):
                raise Rejected("unknown endpoint")
            tools.append(compile_tool(p, candidates[p.endpoint], writes, secrets, today))
        except Rejected as e:
            rejected.append(f"{p.name}: {e}")
    names = {t["name"] for t in tools}
    for tool in tools:  # a reversible that was dropped itself does not count
        if tool.get("reversible") and tool["reversible"] not in names:
            tool["reversible"], tool["human_only"] = None, True
    return Extracted({**manifest, "tools": tools}, session, rejected)
