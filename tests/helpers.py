"""Shared helpers for the tests. No test talks to a real site."""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx2
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

BASE = "https://site.invalid"
PASSWORD = "goed-wachtwoord"


class Site:
    """A fake site for httpx2.MockTransport: answers in order, the last one stays. Remembers every request."""

    def __init__(self, *answers):
        self.answers, self.seen = list(answers), []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer(request) if callable(answer) else answer

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self))


def ok(data=None, status: int = 200, **kwargs) -> httpx2.Response:
    return httpx2.Response(status, json={} if data is None else data, **kwargs)


def tool(name: str = "get", effect: str = "read", **fields) -> dict:
    base = {"name": name, "description": "Test.", "effect": effect, "human_only": False,
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "request": {"method": "GET" if effect == "read" else "POST", "path": f"/{name}"},
            "verifier": "Test.", "evidence": "bundle"}
    if effect == "write":
        base |= {"reversible": None, "human_only": True}
    return base | fields


def manifest(*tools, **fields) -> dict:
    return {"site": "site.invalid", "base": BASE, "auth": {"type": "none"}, "tools": list(tools)} | fields


def demo_site() -> Starlette:
    """A site with a login, a list and a profile. Remembers what was added."""
    items: list[dict] = []

    def signed_in(request: Request) -> bool:
        return request.cookies.get("sid") == "sessie-anna"

    async def login(request: Request):
        if await request.json() != {"email": "anna@demo.invalid", "password": PASSWORD}:
            return JSONResponse({"error": "nee"}, 401)
        response = JSONResponse({"ok": True})
        response.set_cookie("sid", "sessie-anna")
        return response

    async def list_or_add(request: Request):
        if not signed_in(request):
            return JSONResponse({}, 401)
        if request.method == "GET":
            return JSONResponse({"items": items})
        items.append({"id": f"i{len(items) + 1}", "name": (await request.json())["name"]})
        return JSONResponse({"item": items[-1]})

    async def profile(request: Request):
        return JSONResponse({"name": "Anna", "lang": request.query_params["lang"]}) if signed_in(request) \
            else JSONResponse({}, 401)

    site = Starlette(routes=[Route("/login", login, methods=["POST"]),
                             Route("/api/items", list_or_add, methods=["GET", "POST"]),
                             Route("/api/profile", profile)])
    site.state.items = items
    return site


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serve(asgi, port: int | None = None) -> Iterator[str]:
    port = port or free_port()
    server = uvicorn.Server(uvicorn.Config(asgi, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(5)


def demo_manifest(base: str) -> dict:
    return {"site": "demo.invalid", "base": base,
            "auth": {"type": "password", "fields": ["email", "password"],
                     "login": {"method": "POST", "path": "/login", "json": {"email": "{email}", "password": "{password}"}}},
            "tools": [
                {"name": "list_items", "description": "De lijst.", "effect": "read", "human_only": False,
                 "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                 "request": {"method": "GET", "path": "/api/items"}, "response": {"path": "items"},
                 "verifier": "De lijst klopt.", "evidence": "replayed"},
                {"name": "add_item", "description": "Zet iets op de lijst.", "effect": "write", "human_only": True,
                 "reversible": None, "verifier": {"path": "item.id"}, "evidence": "replayed",
                 "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"],
                                  "additionalProperties": False},
                 "request": {"method": "POST", "path": "/api/items", "json": {"name": "{name}"}}}]}
