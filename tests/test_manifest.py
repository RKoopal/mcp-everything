"""The tool format: what check refuses, how render fills in and how extract shapes a response."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from connector.errors import ConnectorError
from connector.manifest import ManifestError, check, extract, load, render
from tests.helpers import BASE, manifest, tool

ROOT = Path(__file__).resolve().parent.parent
SEEDS = sorted(ROOT.glob("sites/*/tools.json")) + [ROOT / "templates/site/tools.json"]
ID = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}


@pytest.mark.parametrize("path", SEEDS, ids=lambda p: p.parent.name)
def test_every_seed_and_the_template_are_valid(path):
    load(path)


@pytest.mark.parametrize("bad, message", [
    (tool(effect="delete"), "unknown effect"),
    (tool(evidence="gok"), "unknown evidence"),
    (tool("Get"), "snake_case"),
    ({"name": "t"}, "missing"),
    (tool(session="ja"), "true or false"),
    (tool("add", "write", reversible=None, human_only=False), "human_only"),
    (tool("add", "write", reversible="x", human_only=False), "runnable verifier"),
    ({k: v for k, v in tool("add", "write").items() if k != "reversible"}, "reversible"),
    (tool(request={"method": "GET", "path": "/{id}"}, input_schema={**ID, "required": []}), "must be required"),
    (tool(request={"method": "GET", "path": "/x", "query": {"q": "{nope}"}}), "unknown values"),
    (tool(request={"method": "POST", "path": "/x", "json": {}, "form": {}}), "json or form"),
    (tool(request={"method": "TRACE", "path": "/x"}), "method"),
    (tool(input_schema={"type": "array"}), "object"),
    (tool(response={"fields": {"a": 1}}), "fields"),
])
def test_check_refuses_unsafe_tools(bad, message):
    with pytest.raises(ManifestError, match=message):
        check(manifest(bad))


@pytest.mark.parametrize("fields, message", [
    ({"base": "https://site.invalid/pad"}, "origin"),
    ({"base": "ftp://site.invalid"}, "origin"),
    ({"runtime": "desktop"}, "runtime"),
    ({"attempts": 9}, "attempts"),
    ({"auth": {"type": "password", "login": {"method": "POST", "path": "/login"}}}, "fields"),
    ({"auth": {"type": "oauth", "authorize_url": "http://x", "token": {"method": "POST", "path": "/t"},
               "refresh": {"method": "POST", "path": "/r"}}}, "https"),
    ({"tools": [tool(), tool()]}, "unique"),
    ({"tools": [tool("add", "write", reversible="remove", human_only=True)]}, "unknown tool"),
])
def test_check_refuses_bad_manifests(fields, message):
    with pytest.raises(ManifestError, match=message):
        check(manifest(**fields))


def test_render_keeps_types_and_drops_missing_optionals():
    request = {"method": "POST", "path": "/lists/{id}", "query": {"input": {"json": {"n": "{n}", "m": "{m}"}}},
               "json": {"id": "{$uuid}", "count": "{n}", "tags": ["{tag}"], "fixed": "query { a }"}}
    out = render(manifest(), request, {"id": "a/b", "n": 3})
    assert out["url"] == f"{BASE}/lists/a%2Fb"
    assert out["params"] == {"input": '{"json":{"n":3}}'}
    assert out["json"]["count"] == 3 and out["json"]["tags"] == [] and out["json"]["fixed"] == "query { a }"
    assert len(out["json"]["id"]) == 36


def test_render_fills_forms_as_text_and_adds_the_site_headers():
    out = render(manifest(headers={"Accept-Version": "11"}), {"method": "POST", "path": "/x", "form": {"on": "{on}"}},
                 {"on": True})
    assert out["data"] == {"on": "true"} and out["headers"] == {"Accept-Version": "11"}


def test_extract_reads_a_script_tag_path_and_fields():
    page = {"props": {"items": [{"id": 1, "price": {"cents": 50}, "seller": "x"}]}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(page)}</script>'
    spec = {"script_id": "__NEXT_DATA__", "path": "props.items", "fields": {"id": "id", "cents": "price.cents"}}
    assert extract(spec, html) == [{"id": 1, "cents": 50}]


def test_extract_calls_a_missing_script_a_possible_block():
    with pytest.raises(ConnectorError, match="blocked"):
        extract({"script_id": "__NEXT_DATA__"}, "<html>verify you are human</html>")


def test_extract_names_graphql_errors_behind_a_200():
    with pytest.raises(ConnectorError, match="Not authorized"):
        extract({"path": "data.me"}, {"data": None, "errors": [{"message": "Not authorized"}]})
