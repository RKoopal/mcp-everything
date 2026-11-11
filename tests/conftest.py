"""The `world` of the server tests: a demo site, a server with only that site, and two users."""
from __future__ import annotations

import json
import os

import pytest

from connector.pipeline import Param, Proposal, ToolProposal
from connector.registry import Registry
from connector.server import Connector, Settings, app
from tests.helpers import demo_manifest, demo_site, free_port, serve


def stub_model(shown) -> Proposal:
    [endpoint] = shown["endpoints"]
    assert endpoint["segments"] == ["api", "profile"]
    return Proposal(tools=[ToolProposal(endpoint=0, name="profile", description="Het profiel.", effect="read",
                                        params=[Param(name="lang", type="string", description="Taal.", required=True,
                                                      location="query:lang")])])


@pytest.fixture
def world(tmp_path):
    """A demo site, a server with only that site, and two users."""
    with serve(demo_site()) as site_url:
        (tmp_path / "seeds" / "demo").mkdir(parents=True)
        (tmp_path / "seeds" / "demo" / "tools.json").write_text(json.dumps(demo_manifest(site_url)))
        port = free_port()
        settings = Settings(data=tmp_path / "data", key=os.urandom(32), allow_private=True,
                            public_url=f"http://127.0.0.1:{port}")
        connector = Connector(settings, ask=stub_model)
        connector.registry = Registry(settings.data / "registry", tmp_path / "seeds")
        tokens = {name: connector.users.add(name) for name in ("anna", "bob")}
        with serve(app(settings, connector), port) as url:
            yield {"url": url, "site": site_url, "connector": connector, "tokens": tokens, "data": settings.data}
