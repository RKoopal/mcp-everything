# Running a server

You run the server and become its admin: you invite the people who use it. The short version is in the
[README](../README.md#run-your-own-server).

## Quick start

You need Docker, an [Anthropic API key](https://console.anthropic.com) (for making tools) and Chrome (for the
extension).

**1. Configure**

```bash
cp .env.example .env
openssl rand -base64 32    # paste as CONNECTOR_SECRET_KEY in .env, and add your ANTHROPIC_API_KEY
```

Keep a copy of `CONNECTOR_SECRET_KEY` somewhere safe: it encrypts the vault, and without it every stored session is
lost.

**2. Start the server and the browser runtime**

```bash
docker compose up -d --build
curl http://127.0.0.1:8000/healthz    # {"ok":true}
```

On Linux, create the data folder for the container user first: `mkdir data && sudo chown 10001 data`.

**3. Make it reachable.** claude.ai and Claude Desktop need a public HTTPS address: put the server behind a reverse
proxy with TLS and set `PUBLIC_URL` to that address. Claude Code also works with `http://127.0.0.1:8000` on your own
computer.

**4. Invite people, or make accounts**

```bash
docker compose exec connector python -m connector.users invite --uses 3 --note "team"   # a link for 3 sign-ups
docker compose exec connector python -m connector.users add alice                       # an account; the token is shown once
docker compose exec connector python -m connector.users password alice                  # set a password; asks twice, hidden
docker compose exec connector python -m connector.users list                            # accounts and invites
```

Send the invite link (or just the code). From there, everyone follows [using.md](using.md), you included.

Accounts you make with `users add` are your team: they have no recording limit. People who join with an invite are
guests, with the limits below. If you invite people you don't know, also turn on the
[review of their tools](#reviewing-tools-from-guests).

> [!IMPORTANT]
> Give people the extension from **your** server, at `/extension.zip`: it has your server's address built in. The
> `extension/` folder in this repository has another server built in. With your address typed in its popup, recording
> works, but your sign-up and login pages cannot talk to it.

### Without Docker

```bash
cp .env.example .env    # fill in CONNECTOR_SECRET_KEY
set -a; source .env; set +a; uv run python -m connector.server
uv run python -m connector.users add alice
```

Without `BROWSER_URL`, sites that need the browser runtime are refused. Everything else works.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `CONNECTOR_SECRET_KEY` | (required) | 32 random bytes in base64. Encrypts the vault and signs the login cookie: lose it and every session is gone |
| `ANTHROPIC_API_KEY` | | Needed to make tools from a recording. Everything else works without it |
| `PUBLIC_URL` | `http://127.0.0.1:8000` | The address users reach. Links, the OAuth issuer and `/extension.zip` use it |
| `DATA_DIR` | `data` (`/data` in Docker) | Accounts, registry and vault |
| `BROWSER_URL` | (none; `http://browser:9222` in Docker) | DevTools address of the headless Chrome |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | Where the server listens |
| `ALLOW_PRIVATE_SITES` | | `1` allows http and private addresses. Tests and local demos only |
| `MAX_USERS` | `25` | All accounts together. After that, no invite works |
| `MAX_RECORDINGS_PER_MONTH` | `5` | Per invited user, last 30 days. Only recordings that called Claude count. With `MAX_USERS` this caps what making tools can cost |
| `REVIEW_GUEST_TOOLS` | | `1` keeps tools recorded by an invited user private to that user until a review |

## Reviewing tools from guests

By default, a new tool is shared with every user at once. With `REVIEW_GUEST_TOOLS=1` in `.env`, a tool recorded by a
guest is visible only to that guest until someone on the team shares it. Turn this on when you invite people you don't
know. Read each description before you share it: it goes to every user's agent, so it must not carry instructions
meant for someone else's agent.

```bash
docker compose exec connector python -m connector.registry pending               # waiting tools, with their descriptions
docker compose exec connector python -m connector.registry share <site>_<tool>   # share one with every user
```

## Deployment

Run the compose file behind a reverse proxy with TLS, set `PUBLIC_URL` to the public address, and keep `.env` and
`data/` on the server: they hold the vault key and the vault. The database upgrades itself on start; back up `data/`
before you update.
