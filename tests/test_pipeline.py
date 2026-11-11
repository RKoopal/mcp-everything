"""The pipeline from recording to tools. The model is faked: the tests check what the code makes of it."""
from __future__ import annotations

import json
import re

import pytest

from connector.errors import ConnectorError
from connector.manifest import check
from connector.pipeline import Extracted, Param, Proposal, ToolProposal, base, extract, sensitive

COOKIE = "sid=SESSIE1234567890abcdef; _abck=BOTCOOKIE; taal=nl"


def entry(method, url, response=None, body=None, cookie=COOKIE, headers=(), mime="application/json"):
    sent = [{"name": "Cookie", "value": cookie}, {"name": "Accept-Version", "value": "3"},
            {"name": "User-Agent", "value": "Chrome"}, *headers]
    return {"request": {"method": method, "url": url, "headers": sent,
                        "postData": {"text": json.dumps(body)} if body is not None else {}},
            "response": {"status": 200, "content": {"mimeType": mime, "text": json.dumps(response)}}}


ENTRIES = [
    entry("GET", "https://www.shop.invalid/api/search?q=fiets&page=1", {"items": [{"id": 1, "name": "fiets"}]},
          headers=[{"name": "X-CSRF-Token", "value": "CSRFWAARDE"}]),
    entry("POST", "https://www.shop.invalid/api/cart", {"cart": {"id": "c1"}}, {"productId": "12345", "quantity": 1}),
    entry("DELETE", "https://www.shop.invalid/api/cart/12345", {"removed": True}),
    entry("GET", "https://www.shop.invalid/api/me", {"email": "anna@voorbeeld.nl"}),
    entry("GET", "https://www.shop.invalid/app.js", mime="text/javascript"),
    entry("POST", "https://collect.tracker.invalid/e", {}, {"user": "anna@voorbeeld.nl"}),
]
SEARCH = ToolProposal(endpoint=0, name="search_products", description="Zoek.", effect="read", result_path="items",
                      params=[Param(name="query", type="string", description="Zoektekst.", required=True,
                                    location="query:q"),
                              Param(name="page", type="integer", description="Pagina.", required=False,
                                    location="query:page")])
ADD = ToolProposal(endpoint=1, name="add_to_cart", description="Leg in de mand.", effect="write",
                   reversible="remove_from_cart", verify_path="cart.id",
                   params=[Param(name="product_id", type="string", description="Id.", required=True,
                                 location="body:/productId"),
                           Param(name="quantity", type="integer", description="Aantal.", required=True,
                                 location="body:/quantity")])
REMOVE = ToolProposal(endpoint=2, name="remove_from_cart", description="Haal uit de mand.", effect="write",
                      reversible="add_to_cart", verify_path="removed",
                      params=[Param(name="product_id", type="string", description="Id.", required=True,
                                    location="path:2")])


class Model:
    """Gives a fixed proposal and remembers what the model got to see."""

    def __init__(self, *tools):
        self.tools, self.shown = list(tools), []

    def __call__(self, shown):
        self.shown.append(json.dumps(shown, ensure_ascii=False))
        return Proposal(tools=self.tools)


def extracted(*tools, entries=ENTRIES, known=None) -> tuple[Extracted, Model]:
    model = Model(*tools)
    return extract({"log": {"entries": entries}}, "shop.invalid", known, model), model


def test_the_model_never_sees_a_secret():
    _, model = extracted(SEARCH)
    for secret in ("SESSIE1234567890abcdef", "BOTCOOKIE", "CSRFWAARDE", "anna@voorbeeld.nl", "tracker"):
        assert secret not in model.shown[0]
    assert '"q": "fiets"' in model.shown[0]


def test_the_session_goes_to_the_vault_without_bot_protection():
    result, _ = extracted(SEARCH)
    assert result.session == {"cookies": {"sid": "SESSIE1234567890abcdef", "taal": "nl"},
                              "headers": {"x-csrf-token": "CSRFWAARDE"}}


def test_the_manifest_is_fixed_by_code_and_holds_no_user_values():
    result, _ = extracted(SEARCH, ADD, REMOVE)
    manifest = result.manifest
    check(manifest)
    assert {k: manifest[k] for k in ("site", "base", "runtime", "auth", "headers")} == {
        "site": "shop.invalid", "base": "https://www.shop.invalid", "runtime": "browser",
        "auth": {"type": "session"}, "headers": {"accept-version": "3"}}
    assert "SESSIE" not in json.dumps(manifest) and "CSRF" not in json.dumps(manifest)


def test_a_lookalike_domain_is_not_the_site_and_the_api_subdomain_is():
    entries = [entry("GET", "https://api.shop.invalid/v1/orders", {"orders": []}),
               entry("GET", "https://evilshop.invalid/api/steal", {"x": 1}),
               entry("GET", "https://evilshop.invalid/api/more", {"x": 1})]
    assert base({"log": {"entries": entries}}, "shop.invalid") == "https://api.shop.invalid"
    _, model = extracted(entries=entries)
    assert "steal" not in model.shown[0] and "orders" in model.shown[0]


def test_a_personal_header_stays_out_of_the_shared_manifest():
    mine = [{"name": "X-User-Email", "value": "anna@voorbeeld.nl"}, {"name": "X-Device", "value": "a8F3kQ9zL2mN7pR4"}]
    result, _ = extracted(SEARCH, entries=[{**e, "request": {**e["request"], "headers": e["request"]["headers"] + mine}}
                                         for e in ENTRIES])
    assert result.manifest["headers"] == {"accept-version": "3"}


def test_tools_keep_the_recorded_request_and_only_the_named_inputs():
    tools = {t["name"]: t for t in extracted(SEARCH, ADD, REMOVE)[0].manifest["tools"]}
    assert tools["search_products"]["request"] == {"method": "GET", "path": "/api/search",
                                                   "query": {"q": "{query}", "page": "{page}"}}
    assert tools["search_products"]["response"] == {"path": "items"}
    assert tools["search_products"]["input_schema"]["required"] == ["query"]
    assert tools["add_to_cart"]["request"]["json"] == {"productId": "{product_id}", "quantity": "{quantity}"}
    assert tools["remove_from_cart"]["request"]["path"] == "/api/cart/{product_id}"


def test_a_reversible_verified_write_runs_without_a_human():
    tools = {t["name"]: t for t in extracted(SEARCH, ADD, REMOVE)[0].manifest["tools"]}
    assert tools["add_to_cart"]["human_only"] is False and tools["add_to_cart"]["verifier"] == {"path": "cart.id"}


def test_a_write_without_its_undo_needs_a_human():
    [add] = extracted(ADD)[0].manifest["tools"]
    assert add["reversible"] is None and add["human_only"] is True


def test_the_model_cannot_call_a_post_a_read():
    [add] = extracted(ADD.model_copy(update={"effect": "read"}))[0].manifest["tools"]
    assert add["effect"] == "write" and add["human_only"] is True


@pytest.mark.parametrize("proposal, reason", [
    (SEARCH.model_copy(update={"endpoint": 99}), "unknown endpoint"),
    (SEARCH.model_copy(update={"params": [SEARCH.params[0].model_copy(update={"location": "query:nope"})]}),
     "is not in the request"),
    (ADD.model_copy(update={"params": [ADD.params[0].model_copy(update={"location": "body:/verzonnen"})]}),
     "is not in the request"),
    (REMOVE.model_copy(update={"params": []}), "looks like an id"),
    (SEARCH.model_copy(update={"name": "Search Products"}), "is not valid|snake_case"),
])
def test_what_does_not_match_the_recording_is_rejected(proposal, reason):
    result, _ = extracted(proposal)
    assert result.manifest["tools"] == []
    assert any(re.search(reason, r) for r in result.rejected), result.rejected


def test_known_endpoints_are_not_shown_again():
    first, _ = extracted(SEARCH)
    second, model = extracted(ADD.model_copy(update={"endpoint": 0}), known=first.manifest)
    shown = json.loads(model.shown[0])["endpoints"]
    assert [e["method"] for e in shown] == ["POST", "DELETE", "GET"]
    assert [t["name"] for t in second.manifest["tools"]] == ["add_to_cart"]


def test_a_recording_without_api_calls_is_an_error():
    with pytest.raises(ConnectorError, match="No API"):
        extracted(entries=[entry("GET", "https://www.shop.invalid/", mime="text/html")])


@pytest.mark.parametrize("value, name, expected", [
    ("fiets", "q", None), ("hunter2", "password", "<secret>"), ("a@b.nl", "x", "<email>"),
    ("eyJhbGciOiJIUzI1NiJ9abc123XYZ", "x", "<token>"), ("a1b2c3d4e5f6a7b8c9d0", "sha256Hash", None),
    ("Jan Jansen", "fullName", "<personal>"), ("Jan", "name", "<personal>"), ("0612345678", "x", "<personal>"),
    ("+31 6 1234 5678", "contact", "<personal>"), ("1234 AB", "x", "<personal>"),
    ("884213", "x-customer-id", "<personal>"), (884213, "customerId", "<personal>"),
    ("NL91ABNA0417164300", "x", "<personal>"), ("Damstraat 1", "street", "<personal>"),
    ("2026-11-05", "x", None), ("12345", "productId", None), (3, "quantity", None), ("search", "operationName", None),
])
def test_sensitive_values_are_recognised(value, name, expected):
    assert sensitive(value, name) == expected


def test_personal_values_stay_out_of_the_shared_manifest():
    personal = [entry("POST", "https://www.shop.invalid/api/order", {"order": {"id": "o1"}},
                      {"productId": "12345", "name": "Jan Jansen", "phone": "0612345678"},
                      headers=[{"name": "X-Customer-Id", "value": "884213"}])]
    order = ToolProposal(endpoint=0, name="place_order", description="Bestel.", effect="write", verify_path="order.id",
                         params=[Param(name="product_id", type="string", description="Id.", required=True,
                                       location="body:/productId")])
    model = Model(order)
    result = extract({"log": {"entries": personal}}, "shop.invalid", ask=model)
    assert result.manifest["tools"] == [] and "looks personal" in result.rejected[0]
    assert "x-customer-id" not in result.manifest["headers"] and result.session["headers"]["x-customer-id"] == "884213"
    assert "Jan" not in model.shown[0] and "0612345678" not in model.shown[0] and "884213" not in model.shown[0]


@pytest.mark.parametrize("query, effect", [
    ("query Cart { cart { id } }", "read"),
    ("{ cart { id } }", "read"),
    ("query Cart { cart { id } } mutation AddItem { add { id } }", "write"),  # operationName picks the mutation
    ("mutation AddItem { add { id } }", "write"),
    ("query A{a},mutation B{d}", "write"),  # GraphQL reads a comma, a comment and a byte-order mark as a space
    ("query A{a}#c\nmutation B{d}", "write"),
    ("{a}\ufeffmutation B{x}", "write"),
    ("query Cart { cart { id } } # mutation in a comment", "read"),
    ("query Me { me { subscription { plan } } }", "read"),  # a field, not an operation
    ('query S { search(text: "} mutation X {") { id } }', "read"),
])
def test_a_graphql_document_with_a_mutation_is_a_write(query, effect):
    body = {"query": query, "operationName": "AddItem", "variables": {}}
    graphql = [entry("POST", "https://www.shop.invalid/graphql", {"data": {"cart": {"id": "c1"}}}, body)]
    read = ToolProposal(endpoint=0, name="cart", description="De mand.", effect="read", params=[])
    [tool] = extract({"log": {"entries": graphql}}, "shop.invalid", ask=Model(read)).manifest["tools"]
    assert tool["effect"] == effect and tool["human_only"] is (effect == "write")


def test_analytics_and_consent_cookies_are_no_login():
    public = [entry("GET", "https://www.shop.invalid/api/search?q=fiets", {"items": []},
                    cookie="_ga=GA1.2.123456789.1700000000; _gid=GA1.2.987654321; OptanonConsent=isGpcEnabled=0&groups=C1")]
    search = ToolProposal(endpoint=0, name="search", description="Zoek.", effect="read",
                          params=[Param(name="query", type="string", description="Zoektekst.", required=True,
                                        location="query:q")])
    result = extract({"log": {"entries": public}}, "shop.invalid", ask=Model(search))
    assert result.manifest["auth"] == {"type": "none"} and result.session == {"cookies": {}, "headers": {}}


def test_headers_chrome_sets_itself_are_no_login():
    # sec-ch-ua-mobile contains 'mobile', but is from the browser: a public site stays without a session.
    public = [entry("GET", "https://www.shop.invalid/api/search?q=fiets", {"items": []}, cookie="",
                    headers=[{"name": "sec-ch-ua-mobile", "value": "?0"}, {"name": "sec-ch-ua-platform", "value": '"macOS"'}])]
    search = ToolProposal(endpoint=0, name="search", description="Zoek.", effect="read",
                          params=[Param(name="query", type="string", description="Zoektekst.", required=True,
                                        location="query:q")])
    result = extract({"log": {"entries": public}}, "shop.invalid", ask=Model(search))
    assert result.manifest["auth"] == {"type": "none"} and result.session == {"cookies": {}, "headers": {}}


@pytest.mark.parametrize("query", [
    {"filter": json.dumps({"email": "anna@voorbeeld.nl"})},
    {"variables": json.dumps({"input": {"customerId": 884213, "phone": "0612345678"}})},
])
def test_personal_values_inside_a_json_string_stay_out_of_the_shared_manifest(query):
    from urllib.parse import urlencode

    graphql = [entry("GET", f"https://www.shop.invalid/api/graphql?{urlencode(query)}", {"data": {"orders": []}})]
    read = ToolProposal(endpoint=0, name="orders", description="Orders.", effect="read", params=[])
    result = extract({"log": {"entries": graphql}}, "shop.invalid", ask=Model(read))
    assert result.manifest["tools"] == [] and "personal or secret" in result.rejected[0]
