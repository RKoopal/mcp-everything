"""Summarizes a HAR recording: `python -m connector.har recordings/site.har [--host bol.com]`.

Shows only endpoints, keys and shapes, never values. That makes the summary safe to
read and share; the HAR itself holds cookies and personal data and stays in recordings/.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ID = re.compile(r"\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}|[0-9a-fA-F]{16,}")
API_TYPES = ("json", "graphql", "xml", "text/plain")
MAX_KEYS = 40


def template(path: str) -> str:
    """/order/123;jsessionid=x becomes /order/{id}."""
    parts = [p.split(";")[0] for p in path.split("/")]
    return "/".join("{id}" if ID.fullmatch(p) else p for p in parts)


def shape(value, depth: int = 5):
    """The shape of JSON: keys and types. Dynamic keys such as ids are merged."""
    if depth == 0:
        return "..."
    if isinstance(value, dict):
        if value and all(ID.fullmatch(str(k)) for k in value):
            return {"{id}": shape(next(iter(value.values())), depth - 1)}
        return {k: shape(v, depth - 1) for k, v in list(value.items())[:MAX_KEYS]}
    if isinstance(value, list):
        return [shape(value[0], depth - 1)] if value else []
    return type(value).__name__


def _json(text: str | None):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def request_body(text: str | None):
    """JSON or a form. Of both we only use the shape."""
    return _json(text) or dict(parse_qsl(text or "")) or None


def _hash(item) -> str:
    """The hash of a persisted query. This is an id of the site, not a value of the user."""
    extensions = item.get("extensions") if isinstance(item, dict) else None
    persisted = extensions.get("persistedQuery") if isinstance(extensions, dict) else None
    return persisted.get("sha256Hash", "") if isinstance(persisted, dict) else ""


def operation(entry: dict, body) -> str | None:
    """The name of a GraphQL operation, or the start of the hash if the site only sends that."""
    names = [q["value"] for q in entry["request"].get("queryString", []) if q["name"] == "operationName"]
    for item in body if isinstance(body, list) else [body]:
        name = (item.get("operationName") if isinstance(item, dict) else None) or _hash(item)[:12]
        names += [name] if name else []
    return ",".join(names) or None


def response_json(content: dict):
    text = content.get("text")
    if text and content.get("encoding") == "base64":
        try:
            text = base64.b64decode(text).decode()
        except ValueError:
            return None
    return _json(text)


def key(entry: dict, body=None) -> tuple:
    """What one endpoint is: method, host, path with {id} and the GraphQL operation."""
    url = urlsplit(entry["request"]["url"])
    return entry["request"]["method"], url.netloc, template(url.path), operation(entry, body)


def kind(entry: dict) -> str:
    mime = entry["response"]["content"].get("mimeType", "").lower()
    if entry["request"]["method"] != "GET" or any(t in mime for t in API_TYPES):
        return "api"
    return "document" if "html" in mime else "static"


def summarize(har: dict, host: str = "") -> dict:
    """{"api": [...], "documents": {path: count}, "static": count}. The API is in order of first use."""
    groups: dict[tuple, dict] = {}
    documents: Counter = Counter()
    static = 0
    for entry in har["log"]["entries"]:
        url = urlsplit(entry["request"]["url"])
        if host not in url.netloc:
            continue
        sort = kind(entry)
        if sort == "static":
            static += 1
            continue
        path = template(url.path)
        query = sorted({q["name"] for q in entry["request"].get("queryString", [])})
        if sort == "document":
            documents[f"{url.netloc}{path}" + ("?" + ",".join(query) if query else "")] += 1
            continue
        body = request_body(entry["request"].get("postData", {}).get("text"))
        group_key = key(entry, body)
        group = groups.setdefault(group_key, {"method": group_key[0], "host": group_key[1], "path": path,
                                              "operation": group_key[3], "hash": _hash(body) or None,
                                              "count": 0, "statuses": Counter(), "query": set(), "pages": set(),
                                              "request": None, "response": None})
        group["count"] += 1
        group["statuses"][entry["response"]["status"]] += 1
        group["query"] |= set(query)
        group["pages"] |= {entry["page"]} if entry.get("page") else set()
        if group["request"] is None and body is not None:
            group["request"] = shape(body)
        if group["response"] is None and (answer := response_json(entry["response"]["content"])) is not None:
            group["response"] = shape(answer)
    api = [{**g, "statuses": dict(g["statuses"]), "query": sorted(g["query"]), "pages": sorted(g["pages"])}
           for g in groups.values()]
    return {"api": api, "documents": dict(documents.most_common()), "static": static}


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m connector.har")
    parser.add_argument("file", type=Path)
    parser.add_argument("--host", default="")
    args = parser.parse_args()
    print(json.dumps(summarize(json.loads(args.file.read_text()), args.host), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
