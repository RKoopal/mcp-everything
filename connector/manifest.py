"""The tool format: checking and applying a manifest.

A manifest is data, not code. That is why the registry can share tools between users without
running other people's code: everything a tool does is in the manifest, and is checked here
before it is stored or run.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import jsonschema

from .errors import ConnectorError

REQUIRED = {"name", "description", "effect", "human_only", "input_schema", "request", "verifier", "evidence"}
EFFECTS = {"read", "preview", "write"}
EVIDENCE = {"replayed", "traffic", "bundle"}
RUNTIMES = {"http", "browser"}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
# Per kind of login: which fields the auth block needs, and which values the requests get.
AUTH = {"none": {}, "session": {}, "password": {"login": None}, "oauth": {"token": {"code"}, "refresh": {"refresh_token"}}}
NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
PLACEHOLDER = re.compile(r"\{(\$?[a-z_][a-z0-9_]*)\}")
BUILTINS = {"$uuid": lambda: str(uuid.uuid4())}
SCRIPT = r'<script[^>]*\bid="{}"[^>]*>(.*?)</script>'
_MISSING = object()


class ManifestError(ValueError):
    pass


# ---------- checking ----------

def _whole(value) -> set[str]:
    """Placeholders in JSON only count when they are the whole string: a GraphQL query is full of braces."""
    if isinstance(value, str):
        match = PLACEHOLDER.fullmatch(value)
        return {match[1]} if match else set()
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return set().union(*map(_whole, items))


def _query_names(value) -> set[str]:
    return set(PLACEHOLDER.findall(value)) if isinstance(value, str) else _whole(value)


def _check_request(spec, where: str, known: set[str], required: set[str]) -> None:
    if not isinstance(spec, dict) or spec.get("method") not in METHODS:
        raise ManifestError(f"{where}: request without a valid method")
    path = spec.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ManifestError(f"{where}: path must start with /")
    if "json" in spec and "form" in spec:
        raise ManifestError(f"{where}: choose json or form, not both")
    for part in ("query", "form"):
        if not isinstance(spec.get(part, {}), dict):
            raise ManifestError(f"{where}: {part} must be an object")
    used = set(PLACEHOLDER.findall(path))
    if unbound := used - required - BUILTINS.keys():
        raise ManifestError(f"{where}: path values {sorted(unbound)} must be required")
    for value in [*spec.get("query", {}).values(), *spec.get("form", {}).values()]:
        used |= _query_names(value)
    used |= _whole(spec.get("json"))
    if unknown := used - known - BUILTINS.keys():
        raise ManifestError(f"{where}: unknown values {sorted(unknown)}")


def _check_verifier(tool: dict) -> None:
    verifier = tool["verifier"]
    if isinstance(verifier, dict):
        if not isinstance(verifier.get("path"), str) or not verifier["path"]:
            raise ManifestError(f"{tool['name']}: verifier without path")
    elif not isinstance(verifier, str):
        raise ManifestError(f"{tool['name']}: verifier is text or {{\"path\": ...}}")
    if tool["effect"] == "write" and not tool["human_only"] and not isinstance(verifier, dict):
        raise ManifestError(f"{tool['name']}: a write needs a runnable verifier")


def check_tool(tool: dict) -> None:
    name = tool.get("name", "?")
    if missing := REQUIRED - tool.keys():
        raise ManifestError(f"{name}: missing {sorted(missing)}")
    if not NAME.fullmatch(name):
        raise ManifestError(f"{name}: name must be snake_case, at most 40 characters")
    if tool["effect"] not in EFFECTS:
        raise ManifestError(f"{name}: unknown effect {tool['effect']}")
    if tool["evidence"] not in EVIDENCE:
        raise ManifestError(f"{name}: unknown evidence {tool['evidence']}")
    if not isinstance(tool["human_only"], bool) or not isinstance(tool.get("session", True), bool):
        raise ManifestError(f"{name}: human_only and session are true or false")
    schema = tool["input_schema"]
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as e:
        raise ManifestError(f"{name}: invalid input_schema: {e.message}") from None
    if schema.get("type") != "object":
        raise ManifestError(f"{name}: input_schema must be an object")
    properties = set(schema.get("properties", {}))
    _check_request(tool["request"], name, properties, set(schema.get("required", [])))
    if tool["effect"] == "write":
        if "reversible" not in tool:
            raise ManifestError(f"{name}: write without 'reversible'")
        if tool["reversible"] is None and not tool["human_only"]:
            raise ManifestError(f"{name}: an irreversible action must be human_only")
    _check_verifier(tool)
    response = tool.get("response", {})
    if not isinstance(response, dict) or not all(isinstance(response.get(k, ""), str) for k in ("path", "script_id")):
        raise ManifestError(f"{name}: response has only text in path and script_id")
    if not all(isinstance(v, str) for v in response.get("fields", {}).values()):
        raise ManifestError(f"{name}: response.fields points every field to a path")


def check(manifest: dict) -> None:
    """Refuses a manifest that does not follow the format."""
    base = urlsplit(manifest.get("base", ""))
    if base.scheme not in ("http", "https") or not base.hostname or base.path not in ("", "/") or base.query:
        raise ManifestError("base must be an origin, such as https://www.site.com")
    if not isinstance(manifest.get("site"), str):
        raise ManifestError("site is missing")
    if manifest.get("runtime", "http") not in RUNTIMES:
        raise ManifestError(f"unknown runtime {manifest.get('runtime')}")
    if manifest.get("attempts", 3) not in range(1, 6):
        raise ManifestError("attempts is 1 to 5")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in manifest.get("headers", {}).items()):
        raise ManifestError("headers must be text")
    auth = manifest.get("auth", {"type": "none"})
    if auth.get("type") not in AUTH:
        raise ManifestError(f"unknown auth {auth.get('type')}")
    for part, values in AUTH[auth["type"]].items():
        if part not in auth:
            raise ManifestError(f"auth {auth['type']} needs {part}")
        if part == "login":
            fields = auth.get("fields")
            if not isinstance(fields, list) or not fields or not all(isinstance(f, str) for f in fields):
                raise ManifestError("auth password needs fields, a list of names")
            values = set(fields)
        _check_request(auth[part], f"auth.{part}", values, set())
    if auth["type"] == "oauth" and not str(auth.get("authorize_url", "")).startswith("https://"):
        raise ManifestError("auth oauth needs an https authorize_url")
    tools = manifest.get("tools")
    if not isinstance(tools, list):
        raise ManifestError("tools must be a list")
    for tool in tools:
        check_tool(tool)
    names = [t["name"] for t in tools]
    if len(names) != len(set(names)):
        raise ManifestError("tool names must be unique")
    for tool in tools:
        if tool.get("reversible") and tool["reversible"] not in names:
            raise ManifestError(f"{tool['name']}: reversible points to an unknown tool")


def load(path: Path | str) -> dict:
    manifest = json.loads(Path(path).read_text())
    check(manifest)
    return manifest


# ---------- applying ----------

def _text(value) -> str:
    return json.dumps(value) if isinstance(value, bool) else str(value)


def _fill_text(template: str, values: dict):
    if any(name not in values for name in PLACEHOLDER.findall(template)):
        return _MISSING
    return PLACEHOLDER.sub(lambda m: _text(values[m[1]]), template)


def _fill_json(template, values: dict):
    """Replaces "{name}" with the value in its own type. A missing optional value is left out."""
    if isinstance(template, str):
        match = PLACEHOLDER.fullmatch(template)
        return values.get(match[1], _MISSING) if match else template
    if isinstance(template, dict):
        filled = {k: _fill_json(v, values) for k, v in template.items()}
        return {k: v for k, v in filled.items() if v is not _MISSING}
    if isinstance(template, list):
        return [v for v in (_fill_json(item, values) for item in template) if v is not _MISSING]
    return template


def _fill_query(template, values: dict):
    """Text is filled in; an object becomes JSON, as in RPC APIs with ?input={...}."""
    if isinstance(template, str):
        return _fill_text(template, values)
    return json.dumps(_fill_json(template, values), separators=(",", ":"))


def render(manifest: dict, request: dict, values: dict) -> dict:
    """Fills in a request. Returns the arguments for `http.request`."""
    values = {name: make() for name, make in BUILTINS.items()} | values
    path = PLACEHOLDER.sub(lambda m: quote(_text(values[m[1]]), safe=""), request["path"])
    out = {"method": request["method"], "url": manifest["base"].rstrip("/") + path,
           "params": {k: v for k, t in request.get("query", {}).items()
                      if (v := _fill_query(t, values)) is not _MISSING},
           "headers": dict(manifest.get("headers", {}))}
    if "json" in request:
        out["json"] = _fill_json(request["json"], values)
    if "form" in request:
        out["data"] = {k: v for k, t in request["form"].items() if (v := _fill_text(t, values)) is not _MISSING}
    return out


def dig(data, path: str):
    """The value at a path like "a.b.0.c", or None."""
    for part in filter(None, path.split(".")):
        if isinstance(data, list) and part.isdigit():
            data = data[int(part)] if int(part) < len(data) else None
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


def extract(spec: dict | None, data):
    """Turns the response into the fixed shape from `response`. A missing path is an error, not an empty answer:
    that way you notice the site changed."""
    if not spec:
        return data
    if script := spec.get("script_id"):
        match = re.search(SCRIPT.format(re.escape(script)), data, re.S) if isinstance(data, str) else None
        if not match:
            raise ConnectorError(f"No <script id=\"{script}\"> in the response. Maybe the site blocked it, or it changed.")
        data = json.loads(match[1])
    if path := spec.get("path"):
        found = dig(data, path)
        if found is None:
            errors = data.get("errors") if isinstance(data, dict) else None  # GraphQL reports errors with HTTP 200
            reason = "; ".join(str(e.get("message", e)) for e in errors)[:300] if isinstance(errors, list) else \
                "The site may have changed."
            raise ConnectorError(f"The response has no '{path}'. {reason}")
        data = found
    if fields := spec.get("fields"):
        def pick(item):
            return {key: dig(item, source) for key, source in fields.items()}
        data = [pick(item) for item in data] if isinstance(data, list) else pick(data)
    return data


def verified(tool: dict, data) -> bool:
    """A runnable verifier: the path must have a non-empty value."""
    verifier = tool["verifier"]
    return not isinstance(verifier, dict) or dig(data, verifier["path"]) not in (None, "", [], {}, False)
