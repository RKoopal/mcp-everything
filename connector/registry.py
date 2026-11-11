"""The shared registry: one manifest per site, for all users.

What user A records of a site, user B can use at once; only the session stays per
user. Except when A is a guest (through an invite) and REVIEW_GUEST_TOOLS=1: then only A sees their
new tools, until someone of the team checks them and shares them with `python -m connector.registry share <site>_<tool>`.
Seeds are in sites/<id>/tools.json: in git and checked by hand. What the pipeline makes goes to
<data>/registry/<id>.json. A seed wins: extracted tools only add to it.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .manifest import ManifestError, check

SEEDS = Path(__file__).resolve().parent.parent / "sites"
PREFIXES = ("www.", "api.", "app.", "m.")
# Without _: the server names tools <site>_<tool>. Short: clients allow names up to 64 characters.
SITE = re.compile(r"[a-z0-9][a-z0-9-]{0,22}")


def host_of(url_or_host: str) -> str:
    host = urlsplit(url_or_host).hostname if "//" in url_or_host else url_or_host
    return (host or "").lower().removeprefix("www.")


def site_id(host: str) -> str:
    """app.wiebetaaltwat.nl -> wiebetaaltwat. A host name has no _, so <id>_<tool> stays unique."""
    host = host_of(host)
    for prefix in PREFIXES:
        host = host.removeprefix(prefix)
    return host.split(".")[0]


def visible(manifest: dict, user_id: str) -> dict:
    """What this user sees: the shared tools, and their own tools that still wait for a review."""
    return {**manifest, "tools": [t for t in manifest["tools"] if t.get("shared", True) or t.get("by") == user_id]}


class Registry:
    # ponytail: reads from disk on every question. Fine for dozens of sites; cache on mtime when there are hundreds.
    def __init__(self, extracted: Path, seeds: Path = SEEDS):
        self.extracted, self.seeds = Path(extracted), Path(seeds)
        self.extracted.mkdir(parents=True, exist_ok=True)

    def _read(self, path: Path) -> dict | None:
        return json.loads(path.read_text()) if path.exists() else None

    def ids(self) -> list[str]:
        seeds = {p.parent.name for p in self.seeds.glob("*/tools.json")}
        return sorted(seeds | {p.stem for p in self.extracted.glob("*.json")})

    def get(self, site: str) -> dict | None:
        if not SITE.fullmatch(site):  # the id sometimes comes from outside (the connect tool): never a path like ../users
            return None
        seed, extracted = self._read(self.seeds / site / "tools.json"), self._read(self.extracted / f"{site}.json")
        if not seed or not extracted:
            return seed or extracted
        names = {t["name"] for t in seed["tools"]}
        return {**seed, "tools": seed["tools"] + [t for t in extracted["tools"] if t["name"] not in names]}

    def find(self, host: str) -> str | None:
        """The site whose base is on this host; www does not count."""
        host = host_of(host)
        return next((site for site in self.ids() if host_of(self.get(site)["base"]) == host), None)

    def free_id(self, host: str) -> str:
        """An id for a new site: short if possible, the whole host if the short one is taken."""
        short = site_id(host)
        return short if short not in self.ids() else host_of(host).replace(".", "-")[:23].rstrip("-")

    @contextmanager
    def _locked(self):
        """Read and write one at a time, also between the server and the CLI: otherwise the last write wins."""
        with open(self.extracted / ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _write(self, site: str, manifest: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.extracted, prefix=f".{site}.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(manifest, indent=2, ensure_ascii=False))
        os.replace(tmp, self.extracted / f"{site}.json")

    def add(self, site: str, manifest: dict, by: str | None = None) -> list[str]:
        """Adds the new tools of an extracted manifest. Existing names stay. Returns the new names.

        With `by` (the id of a guest) the new tools wait for a review: only that guest sees them."""
        if not SITE.fullmatch(site):
            raise ManifestError(f"invalid site-id {site}")
        with self._locked():
            current = self.get(site)
            known = {t["name"] for t in current["tools"]} if current else set()
            new = [{**t, "by": by, "shared": False} if by else t for t in manifest["tools"] if t["name"] not in known]
            stored = self._read(self.extracted / f"{site}.json") or {**manifest, "tools": []}
            merged = {**stored, "tools": stored["tools"] + new}
            check(merged)
            if current:
                check({**current, "tools": current["tools"] + new})  # also valid together with the seed
            self._write(site, merged)
        return [t["name"] for t in new]

    def pending(self) -> list[tuple[str, dict]]:
        """The tools that wait for a review, as (site, tool)."""
        return [(site, t) for site in self.ids() for t in (self._read(self.extracted / f"{site}.json") or {"tools": []})["tools"]
                if t.get("shared") is False]

    def share(self, site: str, name: str) -> bool:
        """After a review: from now on the tool is for everyone."""
        with self._locked():
            stored = self._read(self.extracted / f"{site}.json") if SITE.fullmatch(site) else None
            found = next((t for t in stored["tools"] if t["name"] == name), None) if stored else None
            if not found:
                return False
            found["shared"] = True
            self._write(site, stored)
        return True


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m connector.registry")
    parser.add_argument("command", choices=["pending", "share"])
    parser.add_argument("tool", nargs="?", help="<site>_<tool>, for share")
    args = parser.parse_args()
    registry = Registry(Path(os.environ.get("DATA_DIR", "data")) / "registry")
    if args.command == "pending":
        for site, t in registry.pending():
            request = t["request"]
            print(f"{site}_{t['name']}: {t['effect']}{' human_only' if t['human_only'] else ''} "
                  f"{request['method']} {request['path']} (by {t['by']})\n  {json.dumps(request, ensure_ascii=False)}")
            # Every user's model reads these texts: this is where a prompt injection would be.
            print(f"  description: {t['description']}")
            for param, schema in t["input_schema"].get("properties", {}).items():
                print(f"  {param}: {schema.get('description', '')}")
        return
    if not args.tool or "_" not in args.tool:
        parser.error("share needs <site>_<tool>")
    site, _, name = args.tool.partition("_")
    print("Shared." if registry.share(site, name) else "That tool does not wait for a review.")


if __name__ == "__main__":
    main()
