"""Tests for connector/har.py, with shapes from the real AH and DEGIRO connectors."""
from __future__ import annotations

import base64
import json

from connector.har import shape, summarize, template


def entry(url, method="GET", status=200, mime="application/json", body=None, request=None, query=(), encoded=False):
    text = json.dumps(body) if body is not None else None
    content = {"mimeType": mime, "text": text}
    if encoded:
        content = {"mimeType": mime, "encoding": "base64", "text": base64.b64encode(text.encode()).decode()}
    req = {"url": url, "method": method, "queryString": [{"name": q, "value": "SECRET"} for q in query]}
    if request is not None:
        req["postData"] = {"text": json.dumps(request)}
    return {"request": req, "response": {"status": status, "content": content}}


def har(*entries):
    return {"log": {"entries": list(entries)}}


def test_template_hides_ids_and_sessions():
    assert template("/v5/order/ff7e3bde-31c3-489e-bada-bfdf18630257;jsessionid=SECRET") == "/v5/order/{id}"
    assert template("/product/20988925/overview") == "/product/{id}/overview"


def test_shape_shows_nested_lists():
    """This would have shown the nested search results of DEGIRO at once."""
    assert shape({"products": [[{"id": 1, "name": "x"}]]}) == {"products": [[{"id": "int", "name": "str"}]]}


def test_shape_collapses_dynamic_keys():
    assert shape({"data": {"19748526": {"id": "1"}, "18702138": {"id": "2"}}}) == {"data": {"{id}": {"id": "str"}}}


def test_summary_groups_requests_on_one_endpoint():
    result = summarize(har(entry("https://a.nl/item/1", body={"x": 1}), entry("https://a.nl/item/2", status=500)))
    assert len(result["api"]) == 1
    group = result["api"][0]
    assert group["path"] == "/item/{id}" and group["count"] == 2 and group["statuses"] == {200: 1, 500: 1}


def test_summary_never_contains_values():
    result = summarize(har(entry("https://a.nl/x", method="POST", request={"password": "SECRET"},
                                 body={"token": "SECRET"}, query=["sessionId"])))
    text = json.dumps(result)
    assert "SECRET" not in text
    assert "password" in text and "token" in text and "sessionId" in text


def test_summary_splits_graphql_operations():
    result = summarize(har(entry("https://a.nl/graphql", "POST", request={"operationName": "A"}, body={"data": {}}),
                           entry("https://a.nl/graphql", "POST", request={"operationName": "B"}, body={"data": {}})))
    assert sorted(g["operation"] for g in result["api"]) == ["A", "B"]


def test_summary_names_persisted_queries_by_their_hash():
    request = {"extensions": {"persistedQuery": {"sha256Hash": "abcdef0123456789"}}}
    result = summarize(har(entry("https://a.nl/graphql", "POST", request=request, body={"data": {}})))
    assert result["api"][0]["operation"] == "abcdef012345"


def test_summary_keeps_the_full_hash_because_a_tool_needs_it():
    request = {"operationName": "AddItem", "extensions": {"persistedQuery": {"sha256Hash": "abcdef0123456789"}}}
    group = summarize(har(entry("https://a.nl/graphql", "POST", request=request, body={"data": {}})))["api"][0]
    assert group["operation"] == "AddItem" and group["hash"] == "abcdef0123456789"


def test_summary_shows_query_keys_of_documents():
    result = summarize(har(entry("https://a.nl/s/", mime="text/html", query=["searchtext"])))
    assert result["documents"] == {"a.nl/s/?searchtext": 1}


def test_summary_separates_html_and_static():
    result = summarize(har(entry("https://a.nl/page", mime="text/html"), entry("https://a.nl/app.js", mime="text/javascript")))
    assert result["api"] == [] and result["documents"] == {"a.nl/page": 1} and result["static"] == 1


def test_a_post_with_an_html_answer_stays_an_action():
    """On bol, this is how the cart actions got lost among the pages."""
    request = {"operationName": "AddItem"}
    result = summarize(har(entry("https://a.nl/graphql", "POST", mime="text/html", request=request)))
    assert result["documents"] == {} and result["api"][0]["operation"] == "AddItem"


def test_summary_filters_on_host():
    result = summarize(har(entry("https://a.nl/x", body={}), entry("https://tracker.com/y", body={})), host="a.nl")
    assert [g["host"] for g in result["api"]] == ["a.nl"]


def test_summary_decodes_base64_bodies():
    result = summarize(har(entry("https://a.nl/x", body={"items": []}, encoded=True)))
    assert result["api"][0]["response"] == {"items": []}
